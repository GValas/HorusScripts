#!/usr/bin/env python3
"""
Tests des fonctions pures des deux pipelines — aucun NAS, aucun GPU, aucune
dépendance pip (stdlib + unittest). Ils couvrent précisément les endroits qui
ont produit des bugs silencieux :

  - le nettoyage de noms (suffixe de langue des sous-titres, nom vide,
    fichiers annexes à ne pas toucher) ;
  - le parsing de dates et les fuseaux (une heure UTC prise pour de l'heure
    locale décale les vidéos de 1 à 2 h dans Google Photos) ;
  - les dates déduites du nom de fichier et du dossier parent ;
  - la surcouche de config 00-config.local.py.

Lancement :  python3 -m unittest discover -s tests -v
"""

import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent


def load_module(path: Path, name: str):
    """Charge un script du dépôt sous un nom arbitraire.

    Les scripts portent des noms non importables (chiffres, tirets, « + ») ;
    c'est le même mécanisme que celui utilisé par les pipelines eux-mêmes.

    Chaque pipeline a son propre `_common.py`. En production ils ne se croisent
    jamais (un conteneur par pipeline, un seul dossier monté sur /work), mais
    ces tests les chargent dans le MÊME processus : on purge donc l'entrée
    `_common` du cache d'import et on place le dossier du script en tête de
    sys.path, pour que chaque script résolve bien le sien.
    """
    sys.modules.pop("_common", None)
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(path.parent))


PERSO = ROOT / "src" / "perso-media"
PUBLIC = ROOT / "src" / "public-media"

common_perso = load_module(PERSO / "_common.py", "common_perso")
common_public = load_module(PUBLIC / "_common.py", "common_public")
clean = load_module(PUBLIC / "01-clean-names.py", "clean_names")


def _load_enrich():
    """Charge 02-enrich… en bouchonnant piexif (absent hors conteneur).

    Seules les fonctions de date sont testées ici ; elles ne touchent pas à
    piexif, mais l'import du module en a besoin.
    """
    if "piexif" not in sys.modules:
        fake = types.ModuleType("piexif")
        fake.ImageIFD = types.SimpleNamespace(DateTime=306)
        fake.ExifIFD = types.SimpleNamespace(
            DateTimeOriginal=36867, DateTimeDigitized=36868
        )
        fake.load = lambda *a, **k: {}
        fake.dump = lambda *a, **k: b""
        fake.insert = lambda *a, **k: None
        fake.remove = lambda *a, **k: None
        sys.modules["piexif"] = fake
    return load_module(PERSO / "02-enrich-movies-photos-with-date.py", "enrich")


enrich = _load_enrich()


class TestCleanNames(unittest.TestCase):
    """Nettoyage des noms de films/séries (pipeline public, étape 01)."""

    def test_coupe_au_premier_mot_technique(self):
        self.assertEqual(
            clean.clean_movie_title("/m/Le.Film.2019.1080p.x264-TEAM.mkv"),
            "/m/Le.Film.(2019).mkv",
        )

    def test_annee_mise_entre_parentheses_une_seule_fois(self):
        self.assertEqual(
            clean.clean_movie_title("/m/Deja.Propre.(2019).mkv"),
            "/m/Deja.Propre.(2019).mkv",
        )

    def test_episode_en_majuscules(self):
        self.assertEqual(
            clean.clean_movie_title("/m/Serie.S01E02.French.mkv"),
            "/m/Serie.S01E02.mkv",
        )

    def test_suffixe_de_langue_des_sous_titres_preserve(self):
        """Sans ça, les pistes fr et en visent le même nom : une est perdue."""
        fr = clean.clean_movie_title("/m/Le.Film.2019.1080p.fr.srt")
        en = clean.clean_movie_title("/m/Le.Film.2019.1080p.en.srt")
        self.assertEqual(fr, "/m/Le.Film.(2019).fr.srt")
        self.assertEqual(en, "/m/Le.Film.(2019).en.srt")
        self.assertNotEqual(fr, en)

    def test_sous_titre_forced_conserve_ses_deux_jetons(self):
        self.assertEqual(
            clean.clean_movie_title("/m/Le.Film.2019.1080p.en.forced.srt"),
            "/m/Le.Film.(2019).en.forced.srt",
        )

    def test_nom_entierement_technique_laisse_intact(self):
        """« 1080p.mkv » donnait « .mkv » : un fichier caché au nom vide."""
        self.assertEqual(clean.clean_movie_title("/m/1080p.mkv"), "/m/1080p.mkv")

    def test_fichiers_annexes_non_renommes(self):
        for name in ("/m/poster.jpg", "/m/movie.nfo", "/m/fanart.png"):
            self.assertEqual(clean.clean_movie_title(name), name)

    def test_dossier_au_nom_vide_laisse_intact(self):
        self.assertEqual(clean.clean_movies_dir("/m/..."), "/m/...")

    def test_detection_de_collision(self):
        logs = []
        logger = types.SimpleNamespace(warning=lambda *a: logs.append(a))
        n = clean.detect_collisions(
            ["/m/a.1080p.mkv", "/m/a.720p.mkv"], ["/m/A.mkv", "/m/A.mkv"], logger
        )
        self.assertEqual(n, 1)


class TestDates(unittest.TestCase):
    """Parsing de dates et fuseaux (pipeline perso)."""

    def test_utc_converti_vers_paris(self):
        # ffmpeg écrit creation_time en UTC : le lire comme heure locale
        # décalerait la vidéo de 2 h en été.
        self.assertEqual(
            common_perso.parse_any_date("2023-06-01T10:00:00.000000Z"),
            datetime(2023, 6, 1, 12, 0, 0),
        )

    def test_fraction_de_seconde_ne_masque_pas_le_Z(self):
        with_fraction = common_perso.parse_any_date("2023-06-01T10:00:00.000000Z")
        without = common_perso.parse_any_date("2023-06-01T10:00:00Z")
        self.assertEqual(with_fraction, without)

    def test_decalage_explicite_pris_en_compte(self):
        self.assertEqual(
            common_perso.parse_any_date("2023-01-01T10:00:00+02:00"),
            datetime(2023, 1, 1, 9, 0, 0),  # -> 08:00 UTC -> 09:00 Paris (hiver)
        )

    def test_exif_sans_fuseau_reste_heure_locale(self):
        self.assertEqual(
            common_perso.parse_any_date("2023:06:01 12:00:00"),
            datetime(2023, 6, 1, 12, 0, 0),
        )

    def test_aller_retour_local_utc_local(self):
        local = datetime(2023, 6, 1, 12, 0, 0)
        self.assertEqual(
            common_perso.parse_any_date(common_perso.to_utc_iso(local)), local
        )

    def test_valeurs_invalides(self):
        for value in ("", "   ", "pas une date", None):
            self.assertIsNone(common_perso.parse_any_date(value))


class TestDatesDeduites(unittest.TestCase):
    """Inférence de date depuis le nom de fichier / le dossier (étape 02)."""

    def test_format_compact(self):
        self.assertEqual(
            enrich._date_from_filename(Path("/p/VID_20190615_143022.mkv")),
            b"2019:06:15 14:30:22",
        )

    def test_format_capture_macos(self):
        self.assertEqual(
            enrich._date_from_filename(Path("/p/Capture 2022-06-19 at 21.59.44.jpg")),
            b"2022:06:19 21:59:44",
        )

    def test_date_impossible_rejetee(self):
        self.assertIsNone(
            enrich._date_from_filename(Path("/p/IMG_20191345_143022.jpg"))
        )

    def test_sans_motif(self):
        self.assertIsNone(enrich._date_from_filename(Path("/p/vacances.jpg")))

    def test_dossier_yy_mm(self):
        self.assertEqual(
            enrich._date_from_parent_folder(Path("/p/12.02 - Olivia/a.jpg")),
            b"2012:02:01 00:00:00",
        )

    def test_dossier_annee_sur_4_chiffres_non_confondu(self):
        self.assertIsNone(
            enrich._date_from_parent_folder(Path("/p/2012.02 - Olivia/a.jpg"))
        )

    def test_dossier_mois_invalide(self):
        self.assertIsNone(enrich._date_from_parent_folder(Path("/p/12.99 - x/a.jpg")))


class TestExclusions(unittest.TestCase):
    """Dossiers « _ » et fichiers temporaires, exclus par toutes les étapes."""

    def test_dossier_souligne_exclu(self):
        root = Path("/photos")
        self.assertTrue(
            common_perso.in_excluded_folder(root / "_a_trier" / "x.jpg", root)
        )
        self.assertTrue(
            common_perso.in_excluded_folder(root / "2019" / "_brut" / "x.jpg", root)
        )

    def test_fichier_souligne_non_exclu(self):
        """Seuls les DOSSIERS comptent : « _photo.jpg » à la racine reste traité."""
        root = Path("/photos")
        self.assertFalse(common_perso.in_excluded_folder(root / "_photo.jpg", root))

    def test_temporaires_reconnus(self):
        self.assertTrue(common_perso.is_temp_artifact(Path("/p/film.h265tmp.mkv")))
        self.assertTrue(common_perso.is_temp_artifact(Path("/p/film.datetmp.mkv")))
        self.assertFalse(common_perso.is_temp_artifact(Path("/p/film.mkv")))


class TestScanCache(unittest.TestCase):
    """Cache ffprobe : versionnement, purge, validation par mtime + taille."""

    def test_version_incompatible_ignoree(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "cache.json"
            common_public.save_scan_cache(path, {"a": {"codec": "hevc"}}, 1)
            self.assertEqual(
                common_public.load_scan_cache(path, 1), {"a": {"codec": "hevc"}}
            )
            self.assertEqual(common_public.load_scan_cache(path, 2), {})

    def test_fichier_absent_ou_corrompu(self):
        with TemporaryDirectory() as d:
            self.assertEqual(
                common_public.load_scan_cache(Path(d) / "nope.json", 1), {}
            )
            bad = Path(d) / "bad.json"
            bad.write_text("{pas du json", encoding="utf-8")
            self.assertEqual(common_public.load_scan_cache(bad, 1), {})

    def test_purge_des_entrees_mortes(self):
        cache = {"/a": 1, "/b": 2, "/c": 3}
        dropped = common_public.prune_scan_cache(cache, ["/a", "/c"])
        self.assertEqual(dropped, 1)
        self.assertEqual(set(cache), {"/a", "/c"})

    def test_entree_invalidee_par_mtime(self):
        st = types.SimpleNamespace(st_mtime=1000.7, st_size=42)
        self.assertTrue(
            common_public.cache_entry_valid({"mtime": 1000, "size": 42}, st)
        )
        self.assertFalse(
            common_public.cache_entry_valid({"mtime": 999, "size": 42}, st)
        )
        self.assertFalse(
            common_public.cache_entry_valid({"mtime": 1000, "size": 43}, st)
        )
        self.assertFalse(common_public.cache_entry_valid(None, st))


class TestConfigOverlay(unittest.TestCase):
    """Surcouche 00-config.local.py : elle doit écraser 00-config.py."""

    def _pipeline_dir(self, tmp: str) -> Path:
        d = Path(tmp)
        (d / "00-config.py").write_text(
            'DRY_RUN = True\nNAS_MOUNT = "/mnt/wsl/horus"\nCQ = 26\n', encoding="utf-8"
        )
        (d / "script.py").write_text("", encoding="utf-8")
        return d

    def test_sans_surcouche(self):
        with TemporaryDirectory() as tmp:
            d = self._pipeline_dir(tmp)
            cfg = common_public.load_config(str(d / "script.py"))
            self.assertIs(cfg.DRY_RUN, True)
            self.assertIsNone(getattr(cfg, "_OVERLAY_PATH", None))

    def test_la_surcouche_ecrase(self):
        with TemporaryDirectory() as tmp:
            d = self._pipeline_dir(tmp)
            (d / "00-config.local.py").write_text(
                "DRY_RUN = False\nCQ = 30\n", encoding="utf-8"
            )
            cfg = common_public.load_config(str(d / "script.py"))
            self.assertIs(cfg.DRY_RUN, False)
            self.assertEqual(cfg.CQ, 30)
            self.assertEqual(cfg.NAS_MOUNT, "/mnt/wsl/horus")  # non surchargé
            self.assertTrue(cfg._OVERLAY_PATH.endswith("00-config.local.py"))

    def test_pipeline_dry_run_prime_sur_tout(self):
        import os

        with TemporaryDirectory() as tmp:
            d = self._pipeline_dir(tmp)
            cfg = common_public.load_config(str(d / "script.py"))
            previous = os.environ.get("PIPELINE_DRY_RUN")
            try:
                os.environ["PIPELINE_DRY_RUN"] = "0"
                self.assertFalse(common_public.resolve_dry_run(cfg))
                os.environ["PIPELINE_DRY_RUN"] = "1"
                self.assertTrue(common_public.resolve_dry_run(cfg))
            finally:
                if previous is None:
                    os.environ.pop("PIPELINE_DRY_RUN", None)
                else:
                    os.environ["PIPELINE_DRY_RUN"] = previous


class TestGuiConfigWriter(unittest.TestCase):
    """Écriture de la surcouche par l'interface web."""

    def setUp(self):
        self.server = load_module(ROOT / "src" / "gui" / "server.py", "gui_server")

    def test_valeur_avec_diese_non_tronquee(self):
        """L'ancienne réécriture par regex prenait le « # » pour un commentaire."""
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "00-config.py").write_text(
                "DRY_RUN = True\nNOTIFY_WEBHOOK = None\n", encoding="utf-8"
            )
            self.server.PIPELINES["fake"] = {
                "label": "fake",
                "launcher": "x.sh",
                "config": "00-config.py",
                "steps": None,
                "fields": [
                    {"key": "DRY_RUN", "type": "bool", "label": "d"},
                    {"key": "NOTIFY_WEBHOOK", "type": "str_or_none", "label": "n"},
                ],
            }
            self.server._config_path = lambda p: d / "00-config.py"
            self.server._overlay_path = lambda p: d / "00-config.local.py"

            url = "https://ntfy.sh/canal#tag"
            self.server.write_overlay("fake", {"NOTIFY_WEBHOOK": url})

            cfg = common_public.load_config(str(d / "script.py"))
            self.assertEqual(cfg.NOTIFY_WEBHOOK, url)
            # Le fichier versionné n'a pas bougé.
            self.assertIn("NOTIFY_WEBHOOK = None", (d / "00-config.py").read_text())

    def test_liste_serialisee(self):
        field = {"key": "INPUT_FOLDERS", "type": "list", "label": "f"}
        literal = self.server._literal(field, ["/a/b", "/c/d"])
        self.assertEqual(eval(literal), ["/a/b", "/c/d"])
        self.assertEqual(self.server._literal(field, []), "[]")

    def test_coercition_depuis_le_navigateur(self):
        c = self.server.coerce
        self.assertEqual(c({"type": "int"}, "26"), 26)
        self.assertIsNone(c({"type": "str_or_none"}, "  "))
        self.assertEqual(c({"type": "list"}, "/a\n\n  /b  \n"), ["/a", "/b"])


if __name__ == "__main__":
    unittest.main()
