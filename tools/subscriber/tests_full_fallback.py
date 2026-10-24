"""Signed full-resource fallback against disposable Steam trees; never real MTGA."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import zipfile

from publisher import canonical, fingerprint, package
import github_publish as publication
import package_installer
import tests_subscriber as fixtures


class FullFallbackTests(unittest.TestCase):
    setUp = fixtures.SubscriberTests.setUp
    tearDown = fixtures.SubscriberTests.tearDown
    invoke = fixtures.SubscriberTests.invoke
    sign = fixtures.SubscriberTests.sign
    publish = fixtures.SubscriberTests.publish
    resign = fixtures.SubscriberTests.resign
    game_hashes = fixtures.SubscriberTests.game_hashes

    def add_full_resources(self):
        for row in self.release["resources"]:
            name = row["layer"] + "-r" + str(self.release["revision"]) + ".full"
            shutil.copyfile(self.root / (row["layer"] + "-r" + str(self.release["revision"])), self.feed / name)
            row["full"] = {"name": name, "fingerprint": copy.deepcopy(row["output"])}
        self.resign()

    def unknown_game(self, mixed=False):
        for layer, relative in self.paths.items():
            target = self.game / relative
            if mixed and layer in ("bootstrap_fonts", "font_bundle"):
                shutil.copyfile(self.root / (layer + "-r1"), target)
            else:
                target.write_bytes(b"third-party-Chinese|" + layer.encode() * 25000)

    def assert_refused_without_game_write(self, command="update"):
        before = self.game_hashes()
        result = self.invoke(command, ok=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(before, self.game_hashes())
        return result

    def assert_signed_outputs(self):
        for row in self.release["resources"]:
            self.assertEqual(fingerprint(self.game / row["relative"]), row["output"])

    def test_mixed_old_patch_and_current_fonts_update_and_exact_rollback(self):
        self.add_full_resources()
        self.unknown_game(mixed=True)
        before = self.game_hashes()
        self.invoke("preview")
        self.assertEqual(before, self.game_hashes())
        self.assertFalse((self.state / "installation.json").exists())
        self.invoke("update")
        self.assert_signed_outputs()
        journal = json.loads((self.state / "installation.json").read_text())
        for row in journal["rows"]:
            if row["layer"] in ("card", "client", "resources"):
                self.assertFalse(row["original_available"])
                self.assertEqual(row["before"], before[self.paths[row["layer"]]])
        self.invoke("rollback")
        self.assertEqual(before, self.game_hashes())

    def test_all_unknown_resources_can_be_replaced_without_legacy_record(self):
        self.add_full_resources()
        self.unknown_game()
        before = self.game_hashes()
        self.invoke("update")
        self.assert_signed_outputs()
        self.invoke("rollback")
        self.assertEqual(before, self.game_hashes())

    def test_unknown_current_without_full_still_refused(self):
        self.unknown_game()
        for command in ("preview", "update"):
            self.assert_refused_without_game_write(command)
        self.assertFalse((self.state / "installation.json").exists())

    def test_missing_one_required_full_refuses_before_any_game_write(self):
        self.add_full_resources()
        self.unknown_game()
        del self.release["resources"][-1]["full"]
        self.resign()
        self.assert_refused_without_game_write()

    def test_tampered_full_download_refuses_before_any_game_write(self):
        self.add_full_resources()
        self.unknown_game()
        row = self.release["resources"][-1]
        (self.feed / row["full"]["name"]).write_bytes(b"tampered download")
        self.assert_refused_without_game_write()

    def test_full_fingerprint_must_equal_signed_output(self):
        self.add_full_resources()
        self.unknown_game()
        row = self.release["resources"][0]
        row["full"]["fingerprint"] = copy.deepcopy(row["input"])
        self.resign()
        self.assert_refused_without_game_write()

    def test_full_filename_cannot_escape_feed(self):
        self.add_full_resources()
        self.unknown_game()
        self.release["resources"][0]["full"]["name"] = "../outside.full"
        self.resign()
        self.assert_refused_without_game_write()

    def test_signed_release_tamper_cannot_authorize_full_replacement(self):
        self.add_full_resources()
        self.unknown_game()
        manifest = self.feed / (self.release["id"] + ".json")
        envelope = json.loads(manifest.read_text())
        envelope["signature"] = "A" * 88
        manifest.write_text(json.dumps(envelope))
        self.index["releases"][0]["manifest"]["fingerprint"] = fingerprint(manifest)
        self.sign(self.index, self.feed / "index-testing.json")
        self.assert_refused_without_game_write()

    def test_known_official_inputs_continue_using_deltas(self):
        self.add_full_resources()
        before = self.game_hashes()
        # An inaccessible fallback must not break the normal differential path.
        for row in self.release["resources"]:
            (self.feed / row["full"]["name"]).unlink()
        self.invoke("update")
        self.assert_signed_outputs()
        journal = json.loads((self.state / "installation.json").read_text())
        self.assertTrue(all(row["original_available"] for row in journal["rows"]))
        self.invoke("rollback")
        self.assertEqual(before, self.game_hashes())

    def test_unknown_inputs_do_not_need_delta_files(self):
        self.add_full_resources()
        self.unknown_game()
        for row in self.release["resources"]:
            (self.feed / row["patch"]["name"]).unlink()
        self.invoke("update")
        self.assert_signed_outputs()

    def cache_signed_outputs(self):
        cache = self.state / "patch-cache"
        cache.mkdir(parents=True)
        cached = {}
        for row in self.release["resources"]:
            output = row["output"]
            path = cache / (output["sha256"] + "-" + str(output["bytes"]) + ".delta")
            shutil.copyfile(self.root / (row["layer"] + "-r1"), path)
            cached[row["layer"]] = path
        return cached

    def test_cached_signed_outputs_need_neither_full_manifest_nor_delta_files(self):
        self.unknown_game()
        self.cache_signed_outputs()
        for row in self.release["resources"]:
            self.assertNotIn("full", row)
            (self.feed / row["patch"]["name"]).unlink()
        before = self.game_hashes()
        self.invoke("preview")
        self.assertEqual(before, self.game_hashes())
        self.assertFalse((self.state / "installation.json").exists())
        self.invoke("update")
        self.assert_signed_outputs()
        self.invoke("rollback")
        self.assertEqual(before, self.game_hashes())

    def test_cached_signed_output_with_bad_hash_rejected(self):
        self.add_full_resources()
        self.unknown_game()
        cached = self.cache_signed_outputs()
        cached["card"].write_bytes(b"corrupted cached output")
        for command in ("preview", "update"):
            result = self.assert_refused_without_game_write(command)
            self.assertIn("完整性不匹配", result.stderr)

    def test_cached_signed_output_symlink_and_hardlink_rejected(self):
        self.add_full_resources()
        self.unknown_game()
        cached = self.cache_signed_outputs()["card"]
        original = self.root / "card-r1"
        original_bytes = original.read_bytes()
        for link_type, message in (("symbolic", "符号链接"), ("hard", "硬链接")):
            with self.subTest(link_type=link_type):
                cached.unlink()
                if link_type == "symbolic":
                    cached.symlink_to(original)
                else:
                    os.link(original, cached)
                for command in ("preview", "update"):
                    result = self.assert_refused_without_game_write(command)
                    self.assertIn(message, result.stderr)
                self.assertEqual(original_bytes, original.read_bytes())

    def test_existing_install_without_official_base_upgrades_and_rolls_back(self):
        self.add_full_resources()
        self.unknown_game()
        self.invoke("update")
        first = self.game_hashes()
        self.publish(2)
        self.add_full_resources()
        self.invoke("preview")
        self.assertEqual(first, self.game_hashes())
        self.invoke("update")
        self.assert_signed_outputs()
        self.invoke("rollback")
        self.assertEqual(first, self.game_hashes())

    def test_missing_recorded_official_base_is_not_hidden_by_full(self):
        self.invoke("update")
        first = self.game_hashes()
        journal = json.loads((self.state / "installation.json").read_text())
        for path in (self.state / "transactions" / journal["id"]).glob("original-*"):
            path.unlink()
        self.publish(2)
        self.add_full_resources()
        self.assert_refused_without_game_write()
        self.assertEqual(first, self.game_hashes())

    def test_corrupted_recorded_official_base_is_not_hidden_by_full(self):
        self.invoke("update")
        journal = json.loads((self.state / "installation.json").read_text())
        (self.state / "transactions" / journal["id"] / "original-card").write_bytes(b"corrupted")
        self.publish(2)
        self.add_full_resources()
        self.assert_refused_without_game_write()

    def test_current_signed_outputs_need_neither_full_nor_delta(self):
        for row in self.release["resources"]:
            shutil.copyfile(self.root / (row["layer"] + "-r1"), self.game / row["relative"])
            (self.feed / row["patch"]["name"]).unlink()
        before = self.game_hashes()
        self.invoke("preview")
        self.invoke("update")
        self.assertEqual(before, self.game_hashes())
        self.invoke("rollback")
        self.assertEqual(before, self.game_hashes())

    def test_symlinked_full_source_rejected(self):
        self.add_full_resources()
        self.unknown_game()
        row = self.release["resources"][0]
        artifact = self.feed / row["full"]["name"]
        artifact.unlink()
        artifact.symlink_to(self.root / (row["layer"] + "-r1"))
        self.assert_refused_without_game_write()

    def test_full_fallback_does_not_allow_changed_protected_binary(self):
        self.add_full_resources()
        self.unknown_game()
        (self.game / self.protected[1]["relative"]).write_bytes(b"modified executable")
        self.assert_refused_without_game_write()

    def test_full_fallback_does_not_follow_target_symlink(self):
        self.add_full_resources()
        self.unknown_game()
        target = self.game / self.paths["card"]
        target.unlink()
        outside = self.root / "outside-card"
        outside.write_bytes(b"outside data")
        target.symlink_to(outside)
        result = self.invoke("update", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_bytes(), b"outside data")
        self.assertTrue(target.is_symlink())

    def test_external_patch_after_same_revision_install_can_be_replaced_and_restored(self):
        self.add_full_resources()
        self.unknown_game()
        self.invoke("update")
        self.unknown_game()
        before = self.game_hashes()
        self.invoke("update")
        self.assert_signed_outputs()
        self.invoke("rollback")
        self.assertEqual(before, self.game_hashes())

    def test_interrupted_full_replacement_recovers_exact_previous_files(self):
        self.add_full_resources()
        self.unknown_game()
        before = self.game_hashes()
        result = self.invoke("update", env={"MTGA_TEST_CRASH_AFTER": "card"}, ok=False)
        self.assertEqual(result.returncode, 79, result.stderr + result.stdout)
        self.assertNotEqual(before, self.game_hashes())
        self.invoke("recover")
        self.assertEqual(before, self.game_hashes())
        self.invoke("update")
        self.assert_signed_outputs()

    def build_run(self):
        run = self.root / "publisher-run"
        run.mkdir()
        snapshot = {
            "client_version": self.release["client_version"],
            "steam_build": self.release["steam_build"],
            "data_versions": {}, "files": {}, "manifests": {},
        }
        report = {
            "schema": "MTGA_CN_MAINTAINER_V1", "candidate_files": {},
            "localization": {"static_status": "PASS"},
            "import_compatibility": {"static_status": "PASS"},
            "code_fingerprints": {},
        }
        metadata = {"layers": []}
        for layer, relative in self.paths.items():
            base = run / "official" / relative
            base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.bases[layer], base)
            candidate = run / ("candidate-" + layer)
            shutil.copyfile(self.root / (layer + "-r1"), candidate)
            snapshot["files"][layer] = {"relative": relative, **fingerprint(base)}
            report["candidate_files"][layer] = {
                "relative": relative, "path": str(candidate), **fingerprint(candidate),
            }
            metadata["layers"].append({
                "layer": layer, "relative": relative, "fingerprint": fingerprint(base),
            })
        for index, row in enumerate(self.protected):
            if row["relative"].startswith("MTGA_Data/Downloads/Manifest_"):
                snapshot["manifests"][row["relative"]] = copy.deepcopy(row["fingerprint"])
            else:
                snapshot["files"]["protected-" + str(index)] = {
                    "relative": row["relative"], **row["fingerprint"],
                }
        for name, data in (
            ("snapshot.json", snapshot), ("report.json", report),
            ("official-base.json", metadata),
        ):
            (run / name).write_bytes(canonical(data))
        return run

    def real_network_package(self):
        self.repo = "example/full-update-files"
        release_id = self.release["client_version"] + "-" + self.release["steam_build"] + "-r1"
        self.tag = "mtga-" + release_id
        self.packaged = self.root / "real-package"
        self.packaged_release = package(
            self.build_run(), self.packaged, fixtures.BINARY, self.private,
            self.pub, 1, sequence=1, minimum_client=4, include_full=True,
            feed=f"https://raw.githubusercontent.com/{self.repo}/main/feed",
            asset_base=f"https://github.com/{self.repo}/releases/download/{self.tag}",
        )
        self.public_file = self.root / "publisher-public.txt"
        self.public_file.write_text(self.pub)
        return self.publication_plan()

    def publication_plan(self):
        return publication.publication_plan(
            self.packaged, self.repo, self.tag, fixtures.BINARY, self.public_file,
        )

    def resign_packaged_release(self):
        manifest = self.packaged / (self.packaged_release["id"] + ".json")
        self.sign(self.packaged_release, manifest)
        index = {
            "schema": "MTGA_CN_INDEX_V1", "sequence": 1, "channel": "testing",
            "expires_at": self.index["expires_at"],
            "releases": [{
                key: self.packaged_release[key]
                for key in ("id", "revision", "client_version", "steam_build")
            }],
        }
        index["releases"][0]["manifest"] = {
            "name": manifest.name, "fingerprint": fingerprint(manifest),
        }
        self.sign(index, self.packaged / "index-testing.json")

    def test_real_publisher_copies_five_complete_outputs_for_protocol_four(self):
        plan = self.real_network_package()
        self.assertEqual(plan["release"]["minimum_client"], 4)
        self.assertEqual(len(plan["assets"]), 10)
        for row in plan["release"]["resources"]:
            complete = self.packaged / row["full"]["name"]
            self.assertEqual(fingerprint(complete), row["output"])
            self.assertEqual(complete.read_bytes(), (self.root / (row["layer"] + "-r1")).read_bytes())
            self.assertEqual(row["full"]["fingerprint"], row["output"])
            (self.root / "publisher-run" / ("candidate-" + row["layer"])).write_bytes(b"later source change")
            self.assertEqual(fingerprint(complete), row["output"])
        self.assertEqual(len(self.publication_plan()["assets"]), 10)

    def test_real_publisher_rejects_full_without_protocol_four(self):
        run = self.build_run()
        for minimum in (None, 2, 3):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                package(
                    run, self.root / ("bad-package-" + str(minimum)),
                    fixtures.BINARY, self.private, self.pub, 1,
                    minimum_client=minimum, include_full=True,
                )

    def test_publication_rejects_full_url_outside_target_repo(self):
        self.real_network_package()
        row = self.packaged_release["resources"][0]
        row["full"]["url"] = (
            f"https://github.com/other/repository/releases/download/{self.tag}/" + row["full"]["name"]
        )
        self.resign_packaged_release()
        with self.assertRaises(ValueError):
            self.publication_plan()

    def test_publication_rejects_full_claim_different_from_output(self):
        self.real_network_package()
        row = self.packaged_release["resources"][0]
        row["full"]["fingerprint"] = copy.deepcopy(row["input"])
        self.resign_packaged_release()
        with self.assertRaises(ValueError):
            self.publication_plan()

    def test_publication_rejects_tampered_full_file(self):
        self.real_network_package()
        row = self.packaged_release["resources"][0]
        (self.packaged / row["full"]["name"]).write_bytes(b"tampered publication file")
        with self.assertRaises(ValueError):
            self.publication_plan()

    def test_publication_rejects_full_with_old_protocol(self):
        self.real_network_package()
        self.packaged_release["minimum_client"] = 3
        self.resign_packaged_release()
        with self.assertRaises(ValueError):
            self.publication_plan()

    def test_offline_installer_contains_all_five_signed_complete_resources(self):
        self.packaged = self.root / "local-package"
        self.packaged_release = package(
            self.build_run(), self.packaged, fixtures.BINARY, self.private,
            self.pub, 1, sequence=1, minimum_client=4, include_full=True,
            asset_base=None,
        )
        folder = self.root / "offline-installer"
        package_installer.package(
            fixtures.BINARY, self.packaged / "profile.json", folder,
            local_feed=self.packaged,
        )
        self.assertEqual(len(list((folder / "feed").glob("*.full"))), 5)
        with zipfile.ZipFile(str(folder) + ".zip") as archive:
            names = [name for name in archive.namelist() if name.endswith(".full")]
            self.assertEqual(len(names), 5)
            for row in self.packaged_release["resources"]:
                name = row["full"]["name"]
                self.assertEqual(fingerprint(folder / "feed" / name), row["output"])
                self.assertEqual(
                    archive.read(folder.name + "/feed/" + name),
                    (self.packaged / name).read_bytes(),
                )
        self.unknown_game()
        before = self.game_hashes()
        state = self.root / "offline-installed-state"
        self.invoke(
            "setup", "--profile", str(folder / "profile.json"),
            "--state", str(state), base=False,
        )
        installed_profile = json.loads((state / "profile.json").read_text())
        self.assertEqual(Path(installed_profile["feed"]).resolve(), (folder / "feed").resolve())
        result = subprocess.run(
            [str(state / "mtga-cn"), "preview", "--state", str(state),
             "--steam-library", str(self.library)],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("下载和检查完成", result.stdout)
        self.assertEqual(before, self.game_hashes())
        self.assertFalse((state / "installation.json").exists())
