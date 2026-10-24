"""Resource-generation and signed-output adoption regressions; synthetic Steam trees only."""
import copy
import json
import unittest
from publisher import fingerprint
from tests_subscriber import SubscriberTests

class ResourceRefreshTests(unittest.TestCase):
    # Reuse only fixture helpers, without repeating the original regression suite.
    setUp=SubscriberTests.setUp
    tearDown=SubscriberTests.tearDown
    invoke=SubscriberTests.invoke
    sign=SubscriberTests.sign
    publish=SubscriberTests.publish
    resign=SubscriberTests.resign
    def snapshot(self):
        return {str(p.relative_to(self.game)): fingerprint(p) for p in self.game.rglob("*") if p.is_file() and not p.is_symlink()}

    def refreshed_client(self):
        old=self.game/self.paths["client"]
        new=old.with_name("Raw_ClientLocalization_"+"c"*32+".mtga")
        old.rename(new)
        return old,new

    def test_resource_generation_change_explained_without_writes(self):
        old,new=self.refreshed_client()
        before=self.snapshot()
        for command in ("check","preview","update"):
            result=self.invoke(command,ok=False)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("资源已更新",result.stderr)
            self.assertIn("资源世代",result.stderr)
            self.assertIn(new.name,result.stderr)
            self.assertNotIn("找不到文件",result.stderr)
            self.assertEqual(before,self.snapshot())
            self.assertFalse(old.exists())

    def test_missing_raw_without_replacement_explained(self):
        (self.game/self.paths["client"]).unlink()
        before=self.snapshot()
        result=self.invoke("check",ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("资源尚未就绪",result.stderr)
        self.assertEqual(before,self.snapshot())

    def test_multiple_replacement_files_not_guessed(self):
        old,new=self.refreshed_client()
        new.with_name("Raw_ClientLocalization_"+"d"*32+".mtga").write_bytes(b"another")
        before=self.snapshot()
        result=self.invoke("check",ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("多份活动资源",result.stderr)
        self.assertEqual(before,self.snapshot())
        self.assertFalse(old.exists())

    def test_replacement_symlink_rejected(self):
        old,new=self.refreshed_client()
        new.unlink()
        outside=self.root/"untrusted"
        outside.write_bytes(b"outside-game")
        new.symlink_to(outside)
        before=outside.read_bytes()
        result=self.invoke("check",ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("符号链接",result.stderr)
        self.assertEqual(before,outside.read_bytes())
        self.assertTrue(new.is_symlink())
        self.assertFalse(old.exists())

    def test_malformed_replacement_filename_rejected(self):
        old,new=self.refreshed_client()
        new.rename(new.with_name("Raw_ClientLocalization_bad.mtga"))
        before=self.snapshot()
        result=self.invoke("check",ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("名称格式",result.stderr)
        self.assertEqual(before,self.snapshot())

    def test_same_version_new_signed_resource_path_accepted(self):
        old,new=self.refreshed_client()
        self.paths["client"]=str(new.relative_to(self.game))
        self.publish(2)
        before=self.snapshot()
        self.assertIn("可用版本",self.invoke("check").stdout)
        self.invoke("preview")
        self.assertEqual(before,self.snapshot())

    def put_signed_outputs_without_journal(self):
        for row in self.release["resources"]:
            (self.game/row["relative"]).write_bytes((self.root/(row["layer"]+"-r1")).read_bytes())

    def test_signed_desired_outputs_can_be_adopted_without_fake_official_base(self):
        self.put_signed_outputs_without_journal()
        before=self.snapshot()
        self.invoke("preview")
        self.assertEqual(before,self.snapshot())
        self.assertFalse((self.state/"installation.json").exists())
        self.invoke("update")
        self.assertEqual(before,self.snapshot())
        journal=json.loads((self.state/"installation.json").read_text())
        for row in journal["rows"]:
            self.assertFalse(row["original_available"])
            original=self.state/"transactions"/journal["id"]/("original-"+row["layer"])
            self.assertFalse(original.exists())
            self.assertEqual(row["before"],row["after"])
        self.invoke("rollback")
        self.assertEqual(before,self.snapshot())

    def test_mixed_refreshed_official_client_and_unchanged_signed_outputs(self):
        first=copy.deepcopy(self.release)
        self.put_signed_outputs_without_journal()
        old,new=self.refreshed_client()
        new.write_bytes(self.bases["client"].read_bytes())
        self.paths["client"]=str(new.relative_to(self.game))
        self.publish(2)
        for index,row in enumerate(self.release["resources"]):
            if row["layer"]!="client":
                self.release["resources"][index]=next(x for x in first["resources"] if x["layer"]==row["layer"])
        self.resign()
        before=self.snapshot()
        self.invoke("preview")
        self.assertEqual(before,self.snapshot())
        self.invoke("update")
        self.assertEqual(fingerprint(new),next(x for x in self.release["resources"] if x["layer"]=="client")["output"])
        self.assertFalse(old.exists())
        journal=json.loads((self.state/"installation.json").read_text())
        for row in journal["rows"]:
            if row["layer"]!="client":
                self.assertFalse(row["original_available"])
                self.assertEqual(row["before"],row["after"])
        self.invoke("rollback")
        self.assertEqual(before,self.snapshot())

    def test_upgrade_needing_unavailable_official_base_stops_without_game_writes(self):
        self.put_signed_outputs_without_journal()
        self.invoke("update")
        before=self.snapshot()
        self.publish(2)
        result=self.invoke("preview",ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("缺少这一层的官方重建底稿",result.stderr)
        self.assertEqual(before,self.snapshot())

# Avoid running the base class a second time when unittest discovers this module.
del SubscriberTests
