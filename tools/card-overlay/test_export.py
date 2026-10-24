import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from export_cards import export, mana_text, plain


class TranslationBoundaryTests(unittest.TestCase):
    def fixture(self, root):
        current, donor, client, donor_client = [root / x for x in ["current.db", "donor.db", "client.db", "donor-client.db"]]
        for p in [current, donor]:
            with sqlite3.connect(p) as db:
                db.executescript("""
                CREATE TABLE Versions (Name TEXT,Version TEXT);
                INSERT INTO Versions VALUES ('Data','test');
                CREATE TABLE Localizations_enUS (LocId INT,Formatted INT,Loc TEXT);
                CREATE TABLE Localizations_jaJP (LocId INT,Formatted INT,Loc TEXT);
                CREATE TABLE Cards (GrpId INT,TitleId INT,TypeTextId INT,SubtypeTextId INT,
                  FlavorTextId INT,AbilityIds TEXT,OldSchoolManaText TEXT,Power TEXT,Toughness TEXT,
                  IsToken INT,IsPrimaryCard INT);
                INSERT INTO Cards VALUES (1,10,11,0,12,'100:13','o1oU','','',0,1);
                INSERT INTO Localizations_enUS VALUES
                  (10,1,'Test Card'),(11,1,'Instant'),(12,1,'Original English flavor.'),(13,1,'Draw two cards.');
                INSERT INTO Localizations_jaJP VALUES
                  (10,1,'测试牌'),(11,1,'瞬间'),(12,1,'不应显示的中文背景'),(13,1,'抓两张牌。');
                """)
        for p in [client, donor_client]:
            with sqlite3.connect(p) as db:
                db.execute("CREATE TABLE Loc (Key TEXT,enUS TEXT,jaJP TEXT)")
        return current, donor, client, donor_client

    def test_source_change_keeps_english_and_does_not_modify_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            ps = self.fixture(Path(tmp))
            with sqlite3.connect(ps[0]) as db:
                db.execute("UPDATE Localizations_enUS SET Loc='Draw three cards.' WHERE LocId=13")
            before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in ps]
            card = export(*ps)["cards"][0]
            self.assertEqual(card["rules"], "Draw three cards.")
            self.assertFalse(card["translationComplete"])
            self.assertEqual(card["flavorEn"], "Original English flavor.")
            self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in ps])

    def test_same_name_different_rules_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ps = self.fixture(Path(tmp))
            with sqlite3.connect(ps[0]) as db:
                db.execute("INSERT INTO Localizations_enUS VALUES (14,1,'Draw four cards.')")
                db.execute("INSERT INTO Cards VALUES (2,10,11,0,12,'100:14','o1oU','','',0,1)")
            self.assertEqual(export(*ps)["cards"], [])

    def test_printing_flavor_is_not_guessed_from_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            ps = self.fixture(Path(tmp))
            with sqlite3.connect(ps[0]) as db:
                db.execute("INSERT INTO Localizations_enUS VALUES (14,1,'Different print flavor.')")
                db.execute("INSERT INTO Cards VALUES (2,10,11,0,14,'100:13','o1oU','','',0,1)")
            self.assertEqual(export(*ps)["cards"][0]["flavorEn"], "")

    def test_mana_internal_codes_are_readable(self):
        self.assertEqual(mana_text("oXoUoR"), "[X] [U] [R]")
        self.assertEqual(mana_text("o1oU"), "[1] [U]")
        self.assertEqual(plain("{oBoBoB}: {o(W/B)o(W/B)}"), "[B] [B] [B]: [W/B] [W/B]")


if __name__ == "__main__":
    unittest.main()
