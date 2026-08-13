import contextlib
import io
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from dumatebench.noise import NoiseConfig, NoiseInjector, inject_noise
from dumatebench.noise.cli import main as noise_cli_main


class NoiseInjectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_generates_file_and_data_noise_manifest(self):
        source = self.root / "meeting_agenda.txt"
        source.write_text("Meeting agenda final budget is 1234 and owner is Alice.\n")
        output_dir = self.root / "noise"

        manifest = inject_noise(
            [source],
            NoiseConfig(output_dir=output_dir, seed=42, file_noise_count=3, data_noise_count=2),
        )

        self.assertEqual(manifest["seed"], 42)
        self.assertEqual(len(manifest["records"]), 5)
        self.assertIn("historical_version", manifest["noise_types"])
        self.assertIn("similar_keywords", manifest["noise_types"])
        for record in manifest["records"]:
            noise_path = Path(record["noise_file"])
            self.assertTrue(noise_path.exists(), record)
            self.assertEqual(record["source_file"], str(source))
            self.assertNotEqual(noise_path.read_bytes(), source.read_bytes())

    def test_is_deterministic_for_same_seed(self):
        source = self.root / "answer.txt"
        source.write_text("Alpha source content\n")
        out_a = self.root / "a"
        out_b = self.root / "b"

        manifest_a = NoiseInjector(NoiseConfig(output_dir=out_a, seed=7)).generate([source])
        manifest_b = NoiseInjector(NoiseConfig(output_dir=out_b, seed=7)).generate([source])

        names_a = [Path(record["noise_file"]).name for record in manifest_a["records"]]
        names_b = [Path(record["noise_file"]).name for record in manifest_b["records"]]
        self.assertEqual(names_a, names_b)

    def test_pdf_noise_is_a_minimal_pdf(self):
        source = self.root / "brief.pdf"
        source.write_bytes(b"%PDF-1.4\n% source placeholder\n")

        manifest = inject_noise(
            [source],
            NoiseConfig(output_dir=self.root / "noise", seed=1, file_noise_count=0, data_noise_count=1),
        )

        pdf_path = Path(manifest["records"][0]["noise_file"])
        self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-1.4"))

    def test_office_fallbacks_write_ooxml_zip_files(self):
        for suffix, required_member in [
            (".xlsx", "xl/workbook.xml"),
            (".docx", "word/document.xml"),
            (".pptx", "ppt/presentation.xml"),
        ]:
            source = self.root / f"source{suffix}"
            source.write_bytes(b"placeholder")
            manifest = inject_noise(
                [source],
                NoiseConfig(
                    output_dir=self.root / f"noise{suffix}",
                    seed=2,
                    file_noise_count=0,
                    data_noise_count=1,
                ),
            )
            noise_path = Path(manifest["records"][0]["noise_file"])
            with zipfile.ZipFile(noise_path) as archive:
                self.assertIn(required_member, archive.namelist())

    def test_docx_noise_sanitizes_binary_source_hint(self):
        source = self.root / "binary_source.docx"
        source.write_bytes(b"PK\x03\x04\x00\x00\x08\x00\x00\x00Final\x01Answer")

        manifest = inject_noise(
            [source],
            NoiseConfig(output_dir=self.root / "noise-docx", seed=5, file_noise_count=0, data_noise_count=1),
        )

        noise_path = Path(manifest["records"][0]["noise_file"])
        with zipfile.ZipFile(noise_path) as archive:
            self.assertIn("word/document.xml", archive.namelist())

    def test_audio_noise_is_valid_wav(self):
        source = self.root / "voice.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 100)

        manifest = inject_noise(
            [source],
            NoiseConfig(output_dir=self.root / "noise", seed=9, file_noise_count=0, data_noise_count=1),
        )

        wav_path = Path(manifest["records"][0]["noise_file"])
        with wave.open(str(wav_path), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertGreater(wav.getnframes(), 100)

    def test_cli_writes_manifest(self):
        source = self.root / "report.txt"
        source.write_text("Report final answer\n")
        output_dir = self.root / "generated"
        manifest_path = self.root / "manifest.json"

        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = noise_cli_main(
                [
                    str(source),
                    "--output-dir",
                    str(output_dir),
                    "--manifest",
                    str(manifest_path),
                    "--seed",
                    "3",
                    "--file-noise-count",
                    "1",
                    "--data-noise-count",
                    "1",
                ]
            )

        self.assertEqual(exit_code, 0)
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(manifest["records"]), 2)
        self.assertTrue(all(Path(record["noise_file"]).exists() for record in manifest["records"]))


if __name__ == "__main__":
    unittest.main()
