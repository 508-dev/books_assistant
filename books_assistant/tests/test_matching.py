import unittest

from books_assistant.matching import bank_name, invoice_refs, keyword_in, mentions, vendor_key


class TestVendorKey(unittest.TestCase):
    def test_card_and_feed_formats_agree(self):
        for description in ("GITHUB, INC. GITHUB.COM CA 03/24", "DEBIT_CARD GITHUB, INC. GITHUB.COM CA", "GITHUB"):
            self.assertEqual(vendor_key(description), "GITHUB")

    def test_domains_and_prefixes(self):
        self.assertEqual(vendor_key("ELEST.IO DUBLIN 01/05"), "ELEST")
        self.assertEqual(vendor_key("POS DEBIT ELEST.IO +13022940248"), "ELEST")
        self.assertEqual(vendor_key("RCH-KAGI.COM RCH-KAGI.C"), "KAGI")
        self.assertEqual(vendor_key("WWW.ZENBUSINESS.COM HTTPSWWW.ZENB TX"), "ZENBUSINESS")

    def test_ach_names_and_generic_words(self):
        self.assertEqual(vendor_key("MISC_DEBIT ORIG CO NAME:Wise Inc ORIG ID:9453233521"), "WISE")
        self.assertEqual(vendor_key("MONTHLY SERVICE FEE"), "MONTHLY SERVICE")
        self.assertEqual(vendor_key("Interest (Received)"), "INTEREST RECEIVED")
        self.assertEqual(vendor_key("Online ACH Payment 11006205362 To Taxpros"), "TAXPROS")


class TestKeywordIn(unittest.TestCase):
    def test_whole_words_in_the_merchant_part(self):
        self.assertTrue(keyword_in("INTEREST RECEIVED", "Interest (Received)"))
        self.assertTrue(keyword_in("KAGI", "RCH-KAGI.COM RCH-KAGI.C"))
        self.assertTrue(keyword_in("google", "GOOGLE SVCSexample."))

    def test_prefixes_cannot_trigger_a_rule(self):
        self.assertFalse(keyword_in("DEBIT", "MISC_DEBIT ORIG CO NAME:VENMO ORIG ID:3264681992"))

    def test_partial_words_do_not_match(self):
        self.assertFalse(keyword_in("GIT", "GITHUB"))


class TestInvoiceRefs(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(invoice_refs("acme ACC-SINV-2026-00210Inv ACC-SINV-2026-00210"), ["ACC-SINV-2026-00210"])
        self.assertEqual(invoice_refs("PAYING BILL ACCSINV202600257 VIA RAMP"), ["ACC-SINV-2026-00257"])
        self.assertEqual(invoice_refs("SINV202600267"), ["ACC-SINV-2026-00267"])
        self.assertEqual(invoice_refs("Inv ACC-SINV-2026-00278-1"), ["ACC-SINV-2026-00278-1"])
        self.assertEqual(invoice_refs("SASI - ACC-PINV-2026-00076", "Purchase Invoice"), ["ACC-PINV-2026-00076"])


class TestBankName(unittest.TestCase):
    def test_names(self):
        self.assertEqual(bank_name("Zelle payment from JANE Q DOE 26952253949"), "JANE Q DOE")
        self.assertEqual(bank_name("ヤマダ タロウ (Sent)"), "ヤマダ タロウ")
        self.assertEqual(bank_name("WEN-LI Lin 508.dev LLC TRANSFER-1790495668"), "WEN-LI Lin")
        self.assertEqual(bank_name("ORIG CO NAME:ACME LABS ORIG ID:9121145349"), "ACME LABS")

    def test_not_names(self):
        for description in ("DEBIT_CARD MANDOBOT MANDARIN APP TORONTO ON 07/01", "BANCAMIGA WWW.SITCAVE.C FL 11/23",
                            "ACCT_XFER Online Transfer", "WIRE_INCOMING FEDWIRE CREDIT", "Capital One BILLS"):
            self.assertIsNone(bank_name(description), description)


class TestMentions(unittest.TestCase):
    def test_all_words_of_a_name(self):
        self.assertTrue(mentions("Maria Elena Garcia Lopez (Sent)", "Maria Lopez"))
        self.assertFalse(mentions("Jon Smyth (Sent)", "Jon Smith"))

    def test_short_single_words_need_an_alias(self):
        self.assertFalse(mentions("ACME SWEEP", "ACME"))
        self.assertTrue(mentions("Globex payment", "Globex"))

    def test_other_scripts(self):
        self.assertTrue(mentions("ヤマダ タロウ (Sent)", "ヤマダ タロウ"))
