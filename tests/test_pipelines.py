#!/usr/bin/env python3
"""
Tests des fonctions pures des deux pipelines — aucun NAS, aucun GPU, aucune
dépendance pip (stdlib + unittest). Ils couvrent précisément les endroits qui
ont produit des bugs silencieux :

  - le nettoyage de noms (suffixe de langue des sous-titres, nom vide,
    fichiers annexes à ne pas toucher) ;
  - l'identification des films (motif de renommage, empreinte OpenSubtitles,
    titre/année déduits du nom, sous-titres voisins) ;
  - le déclenchement d'un ré-encodage par le débit (un seuil mal calculé
    supprimerait des originaux pour rien) ;
  - le plan d'allègement audio (supprimer la mauvaise piste ferait perdre une
    langue, ou le son tout entier) ;
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
identify = load_module(PUBLIC / "03-identify-movies.py", "identify_movies")
convert = load_module(PUBLIC / "02-convert-to-h265.py", "convert_h265")
slim = load_module(PUBLIC / "04-slim-audio.py", "slim_audio")


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


class TestBitrate(unittest.TestCase):
    """Seuil de débit de 02 : il décide de ré-encoder, donc de supprimer un
    original. Une mesure impossible ne doit JAMAIS déclencher la conversion."""

    GO = 1024**3

    def test_debit_calcule(self):
        # 24,5 Go sur 2 h -> ~29 Mb/s (le cas réel qui a motivé le réglage).
        rate = convert.bitrate_mbps(int(24.5 * self.GO), 7200)
        self.assertAlmostEqual(rate, 29.2, delta=0.3)

    def test_seuil(self):
        deux_h = 7200
        self.assertTrue(convert.exceeds_bitrate(9 * self.GO, deux_h, 8))
        self.assertFalse(convert.exceeds_bitrate(2 * self.GO, deux_h, 8))

    def test_mesure_impossible_ne_declenche_rien(self):
        """Durée illisible ou taille nulle : on ne ré-encode pas à l'aveugle."""
        self.assertFalse(convert.exceeds_bitrate(9 * self.GO, None, 8))
        self.assertFalse(convert.exceeds_bitrate(9 * self.GO, 0, 8))
        self.assertFalse(convert.exceeds_bitrate(0, 7200, 8))
        self.assertIsNone(convert.bitrate_mbps(9 * self.GO, None))

    def test_seuil_absent_desactive_le_controle(self):
        self.assertFalse(convert.exceeds_bitrate(99 * self.GO, 60, None))
        self.assertFalse(convert.exceeds_bitrate(99 * self.GO, 60, 0))


class TestAudioPlan(unittest.TestCase):
    """Étape 04 : décider quoi faire de chaque piste. Une erreur ici fait
    perdre une langue — ou tout le son — de façon irréversible."""

    # Cas réel de la logithèque : VF compressée + VO lossless.
    SKYWALKER = [
        {"index": 0, "codec": "eac3", "channels": 8, "language": "fre",
         "bitrate": 0.90},
        {"index": 1, "codec": "truehd", "channels": 8, "language": "eng",
         "bitrate": 6.22},
    ]

    def plan(self, tracks, **kw):
        kw.setdefault("max_bitrate", 1.0)
        kw.setdefault("drop_duplicate_languages", False)
        kw.setdefault("max_channels", 6)
        return slim.plan_audio_actions(tracks, **kw)

    def test_lossless_reencode_langues_conservees(self):
        actions = self.plan(self.SKYWALKER)
        self.assertEqual([a["action"] for a in actions], ["copy", "transcode"])
        # 7.1 -> 5.1 : l'encodeur eac3 de ffmpeg ne va pas au-delà de 6 canaux.
        self.assertEqual(actions[1]["channels"], 6)
        self.assertEqual(actions[1]["kbps"], 640)
        # Aucune piste supprimée : les deux langues survivent.
        self.assertNotIn("drop", [a["action"] for a in actions])

    def test_piste_compressee_intacte(self):
        """Une piste sous le plafond n'est jamais touchée."""
        actions = self.plan([self.SKYWALKER[0]])
        self.assertEqual(actions[0]["action"], "copy")

    def test_debit_inconnu_ne_declenche_rien(self):
        """Un débit non mesuré ne doit pas provoquer de ré-encodage."""
        tracks = [{"index": 0, "codec": "dts", "channels": 6, "language": "fre",
                   "bitrate": None}]
        self.assertEqual(self.plan(tracks)[0]["action"], "copy")

    def test_doublons_de_langue_seulement_si_demande(self):
        """Deux pistes « fre » sont souvent deux doublages : on ne suppose pas."""
        tracks = [
            {"index": 0, "codec": "dts", "channels": 6, "language": "fre",
             "bitrate": 2.05},
            {"index": 1, "codec": "dts", "channels": 6, "language": "fre",
             "bitrate": 2.05},
        ]
        actions = self.plan(tracks)
        self.assertNotIn("drop", [a["action"] for a in actions])
        actions = self.plan(tracks, drop_duplicate_languages=True)
        self.assertEqual([a["action"] for a in actions].count("drop"), 1)

    def test_jamais_de_film_muet(self):
        """Un plan qui supprimerait TOUTES les pistes est annulé."""
        tracks = [
            {"index": 0, "codec": "ac3", "channels": 6, "language": "fre",
             "bitrate": 0.45},
            {"index": 1, "codec": "ac3", "channels": 6, "language": "fre",
             "bitrate": 0.45},
        ]
        actions = slim.plan_audio_actions(
            tracks, max_bitrate=1.0, drop_duplicate_languages=True, max_channels=6
        )
        self.assertTrue(any(a["action"] != "drop" for a in actions))

    def test_debit_cible_reduit_en_stereo(self):
        """640 kb/s pour une piste stéréo serait du gaspillage."""
        self.assertEqual(slim.target_bitrate_kbps(6), 640)
        self.assertEqual(slim.target_bitrate_kbps(2), 224)

    def test_langues_preservees(self):
        """Perdre une langue est irréversible ; en gagner une étiquette, non."""
        self.assertTrue(slim.languages_preserved(["fre", "eng"], ["fre", "eng"]))
        # Piste non étiquetée en entrée : ffmpeg pose une étiquette au remux.
        self.assertTrue(slim.languages_preserved(["und"], ["fre"]))
        # Une langue connue qui change ou disparaît : refusé.
        self.assertFalse(slim.languages_preserved(["fre", "eng"], ["fre", "spa"]))
        self.assertFalse(slim.languages_preserved(["fre", "eng"], ["fre"]))

    def test_commande_copie_la_video(self):
        """Garde-fou central : la vidéo doit être copiée, jamais ré-encodée."""
        actions = self.plan(self.SKYWALKER)
        cmd = slim.build_command(Path("/in.mkv"), Path("/out.mkv"), actions)
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertNotIn("libx265", cmd)
        self.assertNotIn("hevc_nvenc", cmd)
        # Les sous-titres et les pièces jointes suivent.
        self.assertIn("0:s?", cmd)
        self.assertIn("0:t?", cmd)


class TestScanCacheEntry(unittest.TestCase):
    """Le cache de scan est écrit par deux chemins (séquentiel et parallèle) ;
    une divergence entre eux a déjà rendu le contrôle de débit inopérant."""

    def test_entree_complete_et_valide(self):
        st = types.SimpleNamespace(st_mtime=1700000000.7, st_size=12345)
        info = {"codec": "hevc", "width": 3840, "height": 2160, "duration": 7200.0}
        entry = convert.scan_cache_entry(st, info)
        # Tous les champs que probe_for_scan relit ensuite.
        self.assertEqual(
            set(entry), {"mtime", "size", "codec", "width", "height", "duration"}
        )
        self.assertEqual(entry["duration"], 7200.0)
        # …et l'entrée doit être reconnue valide pour ce même fichier.
        self.assertTrue(common_public.cache_entry_valid(entry, st))


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


class TestIdentifyMovies(unittest.TestCase):
    """Étape 03 : motif de renommage, empreinte, repli par titre."""

    PATTERN = "{titre}.({yyyy}).{ext}"
    JOKER = {
        "annee": "2019",
        "titre": "Joker",
        "titre_vo": "Joker",
        "ext": "mkv",
    }

    def test_motif_par_defaut(self):
        name, missing = identify.build_new_name(self.PATTERN, self.JOKER)
        self.assertEqual(name, "Joker.(2019).mkv")
        self.assertEqual(missing, [])

    def test_alias_du_motif(self):
        """{année}, {yyyy} et {extension} désignent les mêmes champs."""
        name, _ = identify.build_new_name("{année}.{titre}.{extension}", self.JOKER)
        self.assertEqual(name, "2019.Joker.mkv")
        name, _ = identify.build_new_name("({yyyy}).{titre_vo}.{ext}", self.JOKER)
        self.assertEqual(name, "(2019).Joker.mkv")

    def test_champ_manquant_signale(self):
        """Un titre introuvable ne doit PAS produire « (2019).mkv »."""
        values = {**self.JOKER, "titre": ""}
        name, missing = identify.build_new_name(self.PATTERN, values)
        self.assertEqual(missing, ["titre"])
        self.assertEqual(name, "(2019).mkv")  # jamais appliqué (missing)

    def test_motif_invalide_refuse(self):
        with self.assertRaises(SystemExit):
            identify.validate_pattern("{annee}.{titre}")  # pas d'extension
        with self.assertRaises(SystemExit):
            identify.validate_pattern("{annee}.{acteur}.{ext}")  # champ inconnu
        with self.assertRaises(SystemExit):
            # {realisateur} a été retiré : un motif qui l'utilise doit échouer
            # au démarrage, pas produire des noms amputés.
            identify.validate_pattern("{titre}.{realisateur}.{ext}")

    def test_caracteres_interdits_retires(self):
        self.assertEqual(
            identify.sanitize_component("Mission: Impossible - Fallout"),
            "Mission.Impossible.Fallout",
        )
        self.assertNotIn("/", identify.sanitize_component("Face/Off"))

    def test_espaces_conserves_si_pas_de_remplacement(self):
        self.assertEqual(
            identify.sanitize_component("Le Grand Bleu", space=""), "Le Grand Bleu"
        )

    def test_annee_entre_parentheses_prioritaire(self):
        """« Blade.Runner.2049.(2017) » ne doit pas être daté de 2049."""
        self.assertEqual(
            identify.parse_title_year("Blade.Runner.2049.(2017)"),
            ("Blade Runner 2049", 2017),
        )
        self.assertEqual(identify.parse_title_year("Le.Roi.Lion.2019"),
                         ("Le Roi Lion", 2019))

    def test_empreinte_opensubtitles(self):
        """Fichier de zéros : l'empreinte vaut exactement la taille du fichier."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "film.mkv"
            path.write_bytes(b"\x00" * 200000)
            self.assertEqual(identify.opensubtitles_hash(path), f"{200000:016x}")
            # Trop petit (< 128 Kio) : pas d'empreinte possible.
            path.write_bytes(b"\x00" * 100)
            self.assertIsNone(identify.opensubtitles_hash(path))

    def test_sous_titres_voisins(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            video = d / "Joker.(2019).mkv"
            for name in (
                "Joker.(2019).mkv",
                "Joker.(2019).fr.srt",
                "Joker.(2019).en.forced.srt",
                "Joker.(2019).jpg",  # jaquette : pas un sous-titre
                "Autre.Film.fr.srt",  # autre film : ne suit pas
            ):
                (d / name).write_text("", encoding="utf-8")
            found = {p.name: suffix for p, suffix in identify.companion_files(video)}
            self.assertEqual(
                found,
                {
                    "Joker.(2019).fr.srt": ".fr",
                    "Joker.(2019).en.forced.srt": ".en.forced",
                },
            )
            self.assertNotIn(video.name, found)

    def test_resultat_invraisemblable_ecarte(self):
        """Le repli par titre renvoie TOUJOURS un film : on vérifie qu'il colle."""
        joker = {"titre": "Joker", "titre_vo": "Joker", "annee": "2019"}
        self.assertTrue(identify.match_is_plausible("Joker.(2019)", joker))
        # Titre localisé différent du nom de fichier : le titre ORIGINAL sauve.
        parrain = {"titre": "Le Parrain", "titre_vo": "The Godfather", "annee": "1972"}
        self.assertTrue(identify.match_is_plausible("The.Godfather.(1972)", parrain))
        # Déjà renommé par un run précédent : toujours reconnu, donc inchangé.
        self.assertTrue(identify.match_is_plausible("Joker.(2019)", joker))
        # Titre distribué sous un autre nom : aucun mot commun, mais l'année
        # trouvée est celle du fichier -> la recherche était filtrée dessus.
        poupees = {
            "titre": "Les Poupées russes",
            "titre_vo": "Les Poupées russes",
            "annee": "2005",
        }
        self.assertTrue(identify.match_is_plausible("Russian.Dolls.(2005)", poupees))
        # Film sans rapport : refusé plutôt que renommé n'importe comment.
        avatar = {"titre": "Avatar", "titre_vo": "Avatar", "annee": "2009"}
        self.assertFalse(identify.match_is_plausible("Joker.(2019)", avatar))
        # Sans année dans le nom, seul le recoupement de mots protège.
        eau = {
            "titre": "Avatar : La Voie de l'eau",
            "titre_vo": "Avatar: The Way of Water",
            "annee": "2022",
        }
        self.assertFalse(identify.match_is_plausible("The.Way", eau))

    def test_nom_annee_en_tete(self):
        """Convention déjà en place sur le NAS : « (1989).Batman.mkv »."""
        self.assertEqual(
            identify.parse_title_year("(2008).Batman.The.Dark.Knight"),
            ("Batman The Dark Knight", 2008),
        )

    def test_meilleur_resultat_et_non_le_premier(self):
        """TMDB classe d'abord un documentaire sur « Batman The Dark Knight »."""
        results = [
            {
                "id": 1,
                "title": "Batman Unmasked: The Psychology of The Dark Knight",
                "original_title": "Batman Unmasked: The Psychology of "
                "The Dark Knight",
                "release_date": "2008-07-15",
                "popularity": 9,
            },
            {
                "id": 2,
                "title": "The Dark Knight : Le Chevalier noir",
                "original_title": "The Dark Knight",
                "release_date": "2008-07-16",
                "popularity": 120,
            },
        ]
        best = identify.best_result(results, "(2008).Batman.The.Dark.Knight", 2008)
        self.assertEqual(best["id"], 2)
        self.assertIsNone(identify.best_result([], "peu importe", None))

    def test_suite_non_confondue_avec_l_original(self):
        """« Men in Black III » recouvre tous les mots de « Men.In.Black.1 »."""
        mib3 = {"titre": "Men in Black III", "titre_vo": "Men in Black 3",
                "annee": "2012"}
        self.assertFalse(identify.match_is_plausible("Men.In.Black.1", mib3))
        mib1 = {"titre": "Men in Black", "titre_vo": "Men in Black",
                "annee": "1997"}
        self.assertTrue(identify.match_is_plausible("Men.In.Black.1", mib1))
        # L'inverse est toléré : la bibliothèque numérote des titres que TMDB
        # ne numérote pas.
        reloaded = {"titre": "Matrix Reloaded", "titre_vo": "The Matrix Reloaded",
                    "annee": "2003"}
        self.assertTrue(
            identify.match_is_plausible("Matrix.2.Reloaded.(2003)", reloaded)
        )

    def test_requete_nettoyee(self):
        """Les jetons parasites survivants de 01 ruinent la recherche TMDB."""
        # Le chiffre est CONSERVÉ dans la requête principale (il distingue les
        # épisodes) et n'est retiré que dans la variante de repli.
        variants, _ = identify.query_variants("Men.In.Black.1.Remastered")
        self.assertEqual(variants, ["Men In Black 1", "Men In Black"])
        variants, year = identify.query_variants("Ducobu.L.Eleve.(2011).Vof")
        self.assertEqual((variants, year), (["Ducobu L Eleve"], 2011))
        # Numérotation interne d'une saga : retirée (le titre TMDB ne l'a pas).
        variants, _ = identify.query_variants("Harry.Potter.1.And.The.Chamber")
        self.assertEqual(
            variants, ["Harry Potter 1 And The Chamber", "Harry Potter And The Chamber"]
        )
        # …mais un chiffre FINAL fait partie du titre.
        variants, _ = identify.query_variants("(2004).Spider.Man.2")
        self.assertEqual(variants, ["Spider Man 2"])
        # Nom entièrement parasite : aucune requête (plutôt qu'une au hasard).
        self.assertEqual(identify.query_variants("1080p"), ([], None))

    def test_variante_apres_annee(self):
        """« Coen.(2000).O.Brother » : le vrai titre suit l'année."""
        variants, year = identify.query_variants("Coen.(2000).O.Brother")
        self.assertEqual((variants, year), (["Coen O Brother", "O Brother"], 2000))
        # Variante spéculative : acceptée seulement si l'année correspond.
        brother = {"titre": "O'Brother", "titre_vo": "O Brother, Where Art Thou?",
                   "annee": "2000"}
        self.assertTrue(
            identify.match_is_plausible("Coen.(2000).O.Brother", brother, strict=True)
        )
        mhd = {"titre": "MHD Dro", "titre_vo": "MHD Dro", "annee": "2020"}
        self.assertFalse(
            identify.match_is_plausible("Jurassic.Park.(1993).Mhd", mhd, strict=True)
        )

    def test_difference_de_casse_seule(self):
        """Le partage SMB ignore ces renommages en signalant un succès."""
        self.assertTrue(
            identify.differs_only_by_case(
                "(2022).Thor.Love.And.Thunder.mkv", "(2022).Thor.Love.and.Thunder.mkv"
            )
        )
        self.assertFalse(identify.differs_only_by_case("Joker.mkv", "Joker.mkv"))
        self.assertFalse(identify.differs_only_by_case("Joker.mkv", "Batman.mkv"))

    def test_cache_echec_perime(self):
        vieux = (datetime.now() - identify.MISS_TTL * 2).isoformat(timespec="seconds")
        recent = datetime.now().isoformat(timespec="seconds")
        cache = {
            "a": {"found": True, "titre": "Joker"},
            "b": {"found": False, "when": vieux},
            "c": {"found": False, "when": recent},
        }
        self.assertIsNotNone(identify.cache_get(cache, "a"))
        self.assertIsNone(identify.cache_get(cache, "b"))  # à re-interroger
        self.assertIsNotNone(identify.cache_get(cache, "c"))
        self.assertIsNone(identify.cache_get(cache, "inconnu"))


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
        self.assertEqual(c({"type": "float"}, "0.5"), 0.5)

    def test_etapes_optionnelles_decochees(self):
        """03 et 04 ne doivent pas être cochées par défaut : la première appelle
        des API externes et renomme toute la logithèque, la seconde réécrit les
        fichiers. Seul l'enchaînement historique 01 → 02 l'est."""
        steps = dict(
            (s[0], s[2] if len(s) > 2 else True)
            for s in self.server.PIPELINES["public"]["steps"]
        )
        self.assertEqual(
            steps, {"01": True, "02": True, "03": False, "04": False}
        )

    def test_champs_editables(self):
        keys = {f["key"] for f in self.server.PIPELINES["public"]["fields"]}
        self.assertIn("IDENTIFY_PATTERN", keys)
        self.assertIn("IDENTIFY_TMDB_API_KEY", keys)
        self.assertIn("AUDIO_MAX_BITRATE", keys)
        self.assertIn("AUDIO_TARGET_CODEC", keys)


if __name__ == "__main__":
    unittest.main()
