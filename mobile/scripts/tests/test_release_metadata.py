import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "stage_beta", Path(__file__).parents[1] / "stage-beta.py"
)
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "package": "com.anonymous.bambubridgeapp",
            "signer_sha256": "a" * 64,
            "version_code": 40,
            "version_name": "0.21.0-beta.1",
            "abis": ["arm64-v8a"],
        }

    def validate(self, metadata):
        stage.validate_metadata(
            metadata, signer="a" * 64, version="0.21.0-beta.1", baseline_code=39
        )

    def test_correct_phone_update_is_accepted(self):
        self.validate(self.metadata)

    def test_source_versions_must_match_the_verified_apk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "android/app").mkdir(parents=True)
            version = self.metadata["version_name"]
            (root / "package.json").write_text(json.dumps({"version": version}))
            (root / "app.json").write_text(json.dumps({"expo": {"version": version}}))
            (root / "package-lock.json").write_text(
                json.dumps({"version": version, "packages": {"": {"version": version}}})
            )
            gradle = root / "android/app/build.gradle"
            gradle.write_text(f'versionName "{version}"\nversionCode 40\n')
            stage.validate_version_sources(root, self.metadata)
            gradle.write_text(f'versionName "{version}"\nversionCode 39\n')
            with self.assertRaisesRegex(ValueError, "source version"):
                stage.validate_version_sources(root, self.metadata)

    def test_debug_or_different_signer_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "signer"):
            self.validate({**self.metadata, "signer_sha256": "b" * 64})

    def test_downgrade_or_reused_code_is_rejected(self):
        for code in (38, 39):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "version"):
                self.validate({**self.metadata, "version_code": code})

    def test_wrong_package_version_and_emulator_architecture_are_rejected(self):
        for key, value in (
            ("package", "another.app"),
            ("version_name", "0.20.15"),
            ("abis", ["x86_64"]),
            ("abis", ["arm64-v8a", "x86_64"]),
        ):
            with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                self.validate({**self.metadata, key: value})


if __name__ == "__main__":
    unittest.main()
