"""File parsing: tested with real files generated on the fly with python-docx / openpyxl / python-pptx / reportlab / Pillow.

These libraries are only used by the tests to build the files; the bot itself doesn't depend on them (it parses Office files with the standard library). Tests whose library isn't installed are skipped.
"""

import asyncio
import datetime
import errno
import io
import json
import os
import unittest
import zipfile

import helpers  # noqa: F401  (sets up sys.path)

from dsbot import files
from dsbot.files import classify, decode_text, extract_bytes, read_file


def need(module):
    try:
        return __import__(module)
    except ImportError:
        raise unittest.SkipTest(f"{module} isn't installed")


class Classify(unittest.TestCase):
    def test_kinds(self):
        cases = {
            ("report.pdf", None): "pdf",
            ("weekly-report.docx", None): "docx",
            ("x", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"): "docx",
            ("budget.XLSX", None): "xlsx",
            ("deck.pptx", None): "pptx",
            ("main.ts", None): "text",  # TypeScript, not a video
            ("App.tsx", None): "text",
            ("Dockerfile", None): "text",
            (".gitignore", None): "text",
            ("notes", "text/plain; charset=utf-8"): "text",
            ("data", "application/json"): "text",
            ("mystery.xyz", None): "sniff",
            ("old.doc", None): "unsupported",
            ("old.rtf", None): "unsupported",
            ("song.mp3", None): "unsupported",
            ("clip.mp4", None): "unsupported",
            ("photo.heic", None): "unsupported",
            ("archive.zip", None): "unsupported",
            ("x", "video/quicktime"): "unsupported",
        }
        for (name, ctype), expected in cases.items():
            self.assertEqual(classify(name, ctype)[0], expected, name)
        self.assertIn(".docx", classify("old.doc", None)[1])  # tells them what to do instead

    def test_sniff_size_limit(self):
        self.assertEqual(files.max_bytes_for("sniff", 20_000_000), files.SNIFF_MAX_BYTES)
        self.assertEqual(files.max_bytes_for("pdf", 20_000_000), 20_000_000)


class Text(unittest.TestCase):
    def test_encodings(self):
        self.assertEqual(decode_text("简体中文：天下大势，分久必合，合久必分。".encode("gbk")), "简体中文：天下大势，分久必合，合久必分。")
        self.assertEqual(decode_text("繁體中文：話說天下大勢，分久必合，合久必分。".encode("big5")), "繁體中文：話說天下大勢，分久必合，合久必分。")
        self.assertEqual(decode_text("带 BOM".encode("utf-8-sig")), "带 BOM")
        self.assertEqual(decode_text("UTF-16 文本".encode("utf-16")), "UTF-16 文本")
        self.assertIsNone(decode_text(bytes(range(256))))

    def test_truncation(self):
        text = "\n".join(f"Line {i}: " + "content" * 20 for i in range(2000))
        r = extract_bytes(text.encode(), "long.txt", None, 5000)
        self.assertLessEqual(len(r["text"]), 5000)
        self.assertTrue(r["text"].endswith("content"))  # cut at the end of a line
        self.assertIn("only the first", r["detail"])

    def test_notebook_drops_outputs(self):
        nb = {"cells": [{"cell_type": "code", "source": ["print(1)\n"], "outputs": [{"data": {"image/png": "QUFB" * 5000}}]}]}
        r = extract_bytes(json.dumps(nb).encode(), "a.ipynb", None, 20000)
        self.assertEqual(r["text"], "[code]\nprint(1)")

    def test_binary_with_unknown_extension(self):
        self.assertIn("error", extract_bytes(b"\x00\x01\x02" * 100, "data.xyz", None, 1000))
        self.assertEqual(extract_bytes("no extension".encode(), "README", None, 1000)["text"], "no extension")

    def test_empty(self):
        self.assertEqual(extract_bytes(b"  \n ", "a.txt", None, 100)["detail"], "the file is empty")


class Office(unittest.TestCase):
    def test_docx(self):
        docx = need("docx")
        d = docx.Document()
        d.add_heading("Weekly report", 1)
        d.add_paragraph("Done this week: the login page.")
        d.add_paragraph("Follow up on the contract", style="List Bullet")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "Task", "Progress"
        t.cell(1, 0).text, t.cell(1, 1).text = "Login page", "90%"
        buf = io.BytesIO()
        d.save(buf)
        r = extract_bytes(buf.getvalue(), "weekly-report.docx", None, 20000)
        self.assertEqual(r["kind"], "Word document")
        self.assertEqual(r["text"], "## Weekly report\nDone this week: the login page.\n• Follow up on the contract\nTask | Progress\nLogin page | 90%")

    def test_xlsx(self):
        openpyxl = need("openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Expenses"
        ws.append(["Date", "Item", "Amount"])
        ws.append([datetime.date(2026, 10, 1), "Hot pot", 268.5])
        ws.append([datetime.datetime(2026, 10, 3, 19, 30), "Movie", 120])
        ws["Z5"] = "far away"
        ws2 = wb.create_sheet("Notes")
        ws2["A1"] = "Split the bill"
        ws2["B2"] = True
        buf = io.BytesIO()
        wb.save(buf)
        r = extract_bytes(buf.getvalue(), "budget.xlsx", None, 20000)
        self.assertEqual(
            r["text"],
            "[Sheet: Expenses]\nDate | Item | Amount\n2026-10-01 | Hot pot | 268.5\n2026-10-03 19:30 | Movie | 120\nZ5=far away\n\n"
            "[Sheet: Notes]\nSplit the bill\n | TRUE",
        )
        self.assertEqual(r["detail"], "2 sheets")

    def test_xlsx_truncated(self):
        openpyxl = need("openpyxl")
        wb = openpyxl.Workbook()
        for i in range(500):
            wb.active.append([f"Row {i}", "x" * 30])
        buf = io.BytesIO()
        wb.save(buf)
        r = extract_bytes(buf.getvalue(), "big.xlsx", None, 2000)
        self.assertLessEqual(len(r["text"]), 2100)
        self.assertIn("only the first part was read", r["detail"])

    def test_pptx(self):
        pptx = need("pptx")
        prs = pptx.Presentation()
        s1 = prs.slides.add_slide(prs.slide_layouts[1])
        s1.shapes.title.text = "First slide"
        s1.placeholders[1].text = "Point one\nPoint two"
        s2 = prs.slides.add_slide(prs.slide_layouts[5])
        s2.shapes.title.text = "Summary"
        buf = io.BytesIO()
        prs.save(buf)
        r = extract_bytes(buf.getvalue(), "deck.pptx", None, 20000)
        self.assertEqual(r["text"], "[Slide 1]\nFirst slide\nPoint one\nPoint two\n\n[Slide 2]\nSummary")
        self.assertEqual(r["detail"], "2 slides")

    def test_zip_bomb_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", " " * (files.MAX_XML_BYTES + 1))
        self.assertLess(len(buf.getvalue()), 100_000)  # tiny when compressed, huge when uncompressed
        self.assertEqual(extract_bytes(buf.getvalue(), "bomb.docx", None, 1000), {"error": "the content inside the file is too large to read"})

    def test_broken_files(self):
        self.assertEqual(extract_bytes(b"not a zip", "x.docx", None, 1000), {"error": "the file is corrupted and can't be opened"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w:document")
        self.assertIn("error", extract_bytes(buf.getvalue(), "x.docx", None, 1000))


class Pdf(unittest.TestCase):
    def setUp(self):
        need("pypdf")
        self.reportlab = need("reportlab")

    def _pdf(self, pages, **kw):
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        buf = io.BytesIO()
        c = canvas.Canvas(buf, **kw)
        for lines in pages:
            for i, line in enumerate(lines):
                c.setFont("STSong-Light", 12)
                c.drawString(72, 750 - 20 * i, line)
            c.showPage()
        c.save()
        return buf.getvalue()

    def test_text_pdf(self):
        # one CJK line, so extracting CJK text from a PDF stays covered
        raw = self._pdf([["Page one: hello", "In Chinese: 你好，世界"], ["Page two"]])
        r = extract_bytes(raw, "a.pdf", None, 20000)
        self.assertEqual(r["text"], "[Page 1]\nPage one: hello\nIn Chinese: 你好，世界\n\n[Page 2]\nPage two")
        self.assertEqual((r["detail"], r["images"]), ("2 pages", []))

    def test_truncated_pdf(self):
        raw = self._pdf([[f"Page {p}, line {i}: " + "word " * 8 for i in range(30)] for p in range(10)])
        r = extract_bytes(raw, "a.pdf", None, 3000)
        self.assertLessEqual(len(r["text"]), 3000)
        self.assertRegex(r["detail"], r"^10 pages, text only read up to page \d$")

    def test_encrypted(self):
        from reportlab.lib.pdfencrypt import StandardEncryption

        locked = self._pdf([["Secret"]], encrypt=StandardEncryption("user-pw", "owner-pw"))
        self.assertEqual(extract_bytes(locked, "a.pdf", None, 1000), {"error": "the PDF needs a password to open, so it can't be read"})
        restricted = self._pdf([["Only printing is restricted"]], encrypt=StandardEncryption("", "owner-pw", canPrint=0))
        self.assertIn("Only printing is restricted", extract_bytes(restricted, "a.pdf", None, 1000)["text"])

    def test_scanned_pdf_becomes_images(self):
        from PIL import Image, ImageDraw

        pages = []
        for i in range(4):
            im = Image.new("RGB", (1700, 2200), "white")
            ImageDraw.Draw(im).rectangle([200, 300, 1500, 400], fill="black")
            pages.append(im)
        buf = io.BytesIO()
        pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
        r = extract_bytes(buf.getvalue(), "scan.pdf", None, 20000)
        self.assertEqual(len(r["images"]), files.SCAN_PAGES)
        self.assertTrue(all(u.startswith("data:image/jpeg;base64,") for u in r["images"]))
        self.assertIn("probably a scan", r["detail"])

    def test_corrupt(self):
        self.assertEqual(extract_bytes(b"%PDF-1.4 junk" * 20, "a.pdf", None, 1000), {"error": "the PDF is corrupted and can't be opened"})


class Subprocess(unittest.IsolatedAsyncioTestCase):
    async def test_office_and_pdf_go_through_child_process(self):
        docx = need("docx")
        d = docx.Document()
        d.add_paragraph("Read in the subprocess")
        buf = io.BytesIO()
        d.save(buf)
        result = await read_file(buf.getvalue(), "a.docx", None, 1000, timeout=30)
        self.assertEqual((result.kind, result.text, result.error), ("Word document", "Read in the subprocess", ""))

    async def test_timeout_kills_child(self):
        result = await read_file(b"%PDF-1.4" + b"0" * 1000, "slow.pdf", None, 1000, timeout=0.001)
        self.assertEqual(result.error, "the file is too complex; reading it took too long")

    async def test_child_does_not_get_secrets(self):
        import os

        os.environ["DISCORD_TOKEN"] = "do-not-leak"
        try:
            self.assertNotIn("DISCORD_TOKEN", files._child_env())
        finally:
            del os.environ["DISCORD_TOKEN"]

    async def test_text_stays_in_process(self):
        result = await read_file("hello".encode(), "a.txt", None, 100)
        self.assertEqual((result.kind, result.text), ("text file", "hello"))

    async def test_unsupported(self):
        result = await read_file(b"x", "a.zip", None, 100)
        self.assertEqual(result.error, "archive file; archives can't be unpacked")



class ReviewFixes(unittest.TestCase):
    """Problems found in the second round of review."""

    def test_long_runs_of_spaces_are_fast(self):
        import time

        raw = (" " * 400_000 + "x\n") * 3
        start = time.time()
        r = extract_bytes(raw.encode(), "message.txt", None, 1200)
        self.assertLess(time.time() - start, 2.0)  # the old regex was quadratic here and took tens of minutes
        self.assertIn("only the first", r["detail"])

    def test_big5_larger_than_detection_sample(self):
        line = "繁體中文的舊檔案，話說天下大勢，分久必合，合久必分。\n"
        for pad in range(4):  # must come out right wherever the 200,000-byte cut lands
            raw = ("x" * pad + line * 9000).encode("big5")
            self.assertGreater(len(raw), files.DETECT_SAMPLE)
            self.assertTrue(decode_text(raw).lstrip("x").startswith("繁體中文的舊檔案"), pad)

    def test_short_gbk_csv(self):
        for row in ("姓名,金额\n张三,100\n李四,200\n", "日期,项目,花费\n10月1日,火锅,268\n", "商品,数量\n苹果,3\n香蕉,5\n"):
            self.assertEqual(decode_text(row.encode("gbk")), row)

    def test_utf32_bom(self):
        self.assertEqual(decode_text("UTF-32 文本".encode("utf-32")), "UTF-32 文本")

    def test_lone_surrogates_never_escape(self):
        nb = '{"cells": [{"cell_type": "code", "source": "print(\\"\\ud800\\")"}]}'
        r = extract_bytes(nb.encode(), "a.ipynb", None, 1000)
        r["text"].encode("utf-8")  # doesn't raise UnicodeEncodeError
        json.dumps(r, ensure_ascii=False).encode("utf-8")
        from dsbot.models import FileText

        self.assertEqual(FileText.from_dict({"text": "a\udc80b"}).text, "a?b")

    def test_password_protected_office(self):
        r = extract_bytes(files.OLE_MAGIC + b"\x00" * 500, "secret.docx", None, 1000)
        self.assertIn("password-protected", r["error"])

    def test_docx_main_part_elsewhere(self):
        xml = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Somewhere else</w:t></w:r></w:p></w:body></w:document>"
        )
        rels = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document2.xml"/></Relationships>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("_rels/.rels", rels)
            z.writestr("word/document2.xml", xml)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], "Somewhere else")

    def test_docx_text_box_not_duplicated(self):
        ns = (
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        )
        box = "<w:txbxContent><w:p><w:r><w:t>Text in a text box</w:t></w:r></w:p></w:txbxContent>"
        xml = (
            f"<w:document {ns}><w:body><w:p><w:r><w:t>Body text</w:t></w:r><w:r><mc:AlternateContent>"
            f"<mc:Choice>{box}</mc:Choice><mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p>"
            "</w:body></w:document>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], "Body text\nText in a text box")

    def test_pptx_soft_line_break(self):
        pptx = need("pptx")
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = "Price:\vfree"
        buf = io.BytesIO()
        prs.save(buf)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.pptx", None, 1000)["text"], "[Slide 1]\nPrice:\nfree")

    def _xlsx(self, rows_xml, styles=None, shared=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
            )
            z.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/></Relationships>',
            )
            if styles:
                z.writestr("xl/styles.xml", styles)
            z.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                + rows_xml
                + "</sheetData></worksheet>",
            )
        return buf.getvalue()

    def test_huge_sheet_is_streamed(self):
        row = '<row r="{0}"><c r="A{0}" t="inlineStr"><is><t>Row {0}: some data, a bit long, a bit long, a bit long</t></is></c></row>'
        raw = self._xlsx("".join(row.format(i) for i in range(1, 600_001)))
        self.assertLess(len(raw), 20_000_000)
        r = extract_bytes(raw, "big.xlsx", None, 2000)
        self.assertTrue(r["text"].startswith("[Sheet: Sheet1]\nRow 1: some data"))
        self.assertIn("only the first part was read", r["detail"])
        self.assertLessEqual(len(r["text"]), 2000)

    def test_first_row_longer_than_budget(self):
        raw = self._xlsx('<row r="1"><c r="A1" t="inlineStr"><is><t>' + "long" * 700 + "</t></is></c></row>")
        r = extract_bytes(raw, "a.xlsx", None, 2000)
        self.assertTrue(r["text"].startswith("[Sheet: Sheet1]\nlonglonglong"))
        self.assertLessEqual(len(r["text"]), 2000)
        self.assertIn("only the first part was read", r["detail"])

    def test_weird_numbers_and_percent(self):
        styles = (
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cellXfs><xf numFmtId="0"/><xf numFmtId="9"/><xf numFmtId="14"/></cellXfs></styleSheet>'
        )
        raw = self._xlsx(
            '<row r="1"><c r="A1"><v>inf</v></c><c r="B1"><v>NaN</v></c><c r="C1"><v>1e400</v></c>'
            '<c r="D1" s="1"><v>0.256</v></c><c r="E1" s="2"><v>nan</v></c><c r="F1" s="2"><v>46000</v></c></row>',
            styles=styles,
        )
        self.assertEqual(extract_bytes(raw, "a.xlsx", None, 1000)["text"], "[Sheet: Sheet1]\ninf | nan | inf | 25.6% | nan | 2025-12-09")

    def test_pdf_blank_last_page_is_not_reported_as_unread(self):
        need("pypdf")
        need("reportlab")
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        for text in ("Page one has some text on it.", "Page two has some text too.", None):
            if text:
                c.drawString(72, 750, text)
            c.showPage()
        c.save()
        self.assertEqual(extract_bytes(buf.getvalue(), "a.pdf", None, 20000)["detail"], "3 pages")


class ChildIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_child_runs_as_nobody_when_root(self):
        import os
        import sys

        if os.name != "posix" or os.geteuid() != 0:
            self.skipTest("privileges are only dropped when running as root")
        kwargs = {"user": files.CHILD_UID, "group": files.CHILD_UID, "extra_groups": []}  # what _child_user() passes when dropping privileges
        code = (
            "import os\n"
            "print(os.getuid())\n"
            "try:\n"
            "    open(f'/proc/{os.getppid()}/environ', 'rb').read(); print('leak')\n"
            "except OSError:\n"
            "    print('blocked')\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, env=files._child_env(), cwd="/", **kwargs
        )
        out, _ = await proc.communicate()
        self.assertEqual(out.decode().split(), [str(files.CHILD_UID), "blocked"])

    async def test_text_also_goes_through_child(self):
        calls = []
        original = files._run_child

        async def spy(*args, **kw):
            calls.append(args[1])
            return await original(*args, **kw)

        files._run_child = spy
        try:
            # non-ASCII text (the emoji is a surrogate pair in the child's JSON output) must come back intact
            result = await read_file("hello 👋".encode(), "a.txt", None, 100)
        finally:
            files._run_child = original
        self.assertEqual((result.text, calls), ("hello 👋", ["a.txt"]))


class ThirdRoundFixes(unittest.TestCase):
    """Problems found when re-checking after the second round of fixes."""

    def test_korean_and_japanese_legacy_encodings(self):
        cases = [
            ("안녕하세요. 오늘 회의는 오후 세 시에 시작합니다.\n자료는 미리 읽어 주세요.\n", "cp949"),
            ("대한민국(大韓民國)은 동아시아의 한반도 남부에 위치한 나라이다. 수도는 서울특별시이다.\n", "euc_kr"),
            ("ㅋㅋㅋ 진짜 웃기다\n오늘 저녁 뭐 먹을래?\n치킨 ㄱㄱ\n", "cp949"),
            ("こんにちは。今日の会議は午後三時から始まります。\n資料を事前に読んでおいてください。\n", "cp932"),
            ("東京都千代田区丸の内一丁目\n大阪府大阪市北区梅田三丁目\n", "cp932"),
            ("[00:12.34]君と初めて会った日\n[00:18.90]空はとても青かった\n", "euc_jp"),
            # game scripts full of symbols: the encoding detector gives up on them, so they rely on the fallback that tries encodings one by one
            ("【主人公】「ここはどこだ……？」\n【少女】「やっと目が覚めたのね」\n", "cp932"),
            ("【主人公】「ここはどこだ……？」\n【少女】「やっと目が覚めたのね」\n", "euc_jp"),
        ]
        for text, encoding in cases:
            self.assertEqual(decode_text(text.encode(encoding)), text, (encoding, text[:12]))

    def test_detector_is_trusted_when_there_is_no_chinese_reading(self):
        pos = "ｺｰﾄﾞ,ｼｮｳﾋﾝﾒｲ,ﾀﾝｶ,ｽｳﾘｮｳ\r\n" + "".join(f"{i:04d},ﾘﾝｺﾞ,{100 + i % 50},{i % 9 + 1}\r\n" for i in range(1500))
        statute = "大韓民國 憲法 第一條 大韓民國은 民主共和國이다. 第二條 國民의 要件은 法律로 定한다.\n" * 300
        for text, encoding in ((pos, "cp932"), (statute, "cp949")):
            self.assertEqual(decode_text(text.encode(encoding)), text, encoding)

    def test_short_korean(self):
        for text in ("연결 보여주기", "%s에서 계속", "\n--- 자동-명령 ---"):
            self.assertEqual(decode_text(text.encode("cp949")), text)

    def test_big5_with_a_private_character(self):
        line = "學號,姓名,班級\n110301,陳冠宇,三年一班\n110302,劉怡君,三年一班\n" * 20
        raw = line.encode("big5")
        i = raw.index("怡".encode("big5"))
        raw = raw[:i] + b"\x8f\xfe" + raw[i + 2 :]  # an end-user-defined character (Windows EUDC area) that isn't in the standard Big5 table
        self.assertTrue(decode_text(raw).startswith("學號,姓名,班級\n110301,陳冠宇"))

    def test_short_chinese_is_not_taken_for_korean_or_japanese(self):
        cases = [
            ("# 读取用户列表\nusers = load_users()\n", "gbk"),
            ("名称,数量,单价\n苹果,3,5.5\n香蕉,12,2.8\n", "gbk"),
            ("姓名,年齡,城市\n陳志明,29,台北\n林美玲,34,高雄\n黃建國,41,台中\n", "big5"),
            ("你好，今天的會議下午三點開始。\n請提前閱讀資料。\n", "big5"),
            ("1\n00:00:01,000 --> 00:00:03,000\n你要去哪裡？\n", "big5"),
            ("E417: 缺少參數: %s", "big5"),
            ("\n# 全域變數:\n", "big5"),
            ("火鍋店訂位。\n", "big5"),
        ]
        for text, encoding in cases:
            self.assertEqual(decode_text(text.encode(encoding)), text, (encoding, text[:12]))

    def test_text_box_in_table_cell_is_read_once(self):
        ns = (
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        )
        box = "<w:txbxContent><w:p><w:r><w:t>in a box</w:t></w:r></w:p></w:txbxContent>"
        cell = (
            "<w:tc><w:p><w:r><w:t>cell</w:t></w:r><w:r><mc:AlternateContent>"
            f"<mc:Choice>{box}</mc:Choice><mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p></w:tc>"
        )
        xml = (
            f"<w:document {ns}><w:body><w:tbl><w:tr>{cell}"
            "<w:tc><w:p><w:r><w:t>right</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], "cell in a box | right")

    def test_wrapped_rows_and_cells(self):
        ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

        def para(t):
            return f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"

        cases = {
            f"<w:tbl><w:tr><w:tc><w:customXml w:element='name'>{para('Alex')}</w:customXml></w:tc><w:tc>{para('100')}</w:tc></w:tr></w:tbl>": "Alex | 100",
            f"<w:tbl><w:tr><w:sdt><w:sdtContent><w:tc>{para('Alex')}</w:tc></w:sdtContent></w:sdt><w:tc>{para('100')}</w:tc></w:tr></w:tbl>": "Alex | 100",
            f"<w:tbl><w:tr><w:tc>{para('Name')}</w:tc></w:tr><w:sdt><w:sdtContent><w:tr><w:tc>{para('Alex')}</w:tc></w:tr></w:sdtContent></w:sdt></w:tbl>": "Name\nAlex",
            f"<w:customXml w:element='x'>{para('Body text')}</w:customXml>": "Body text",
        }
        for body, expected in cases.items():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
            self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], expected)

    def test_readers_say_when_they_cut_the_file(self):
        self.assertTrue(extract_bytes(("x" * 3000).encode(), "a.txt", None, 1000)["truncated"])
        self.assertFalse(extract_bytes("short".encode(), "a.txt", None, 1000)["truncated"])
        xlsx = ReviewFixes()._xlsx("".join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>Row {i}</t></is></c></row>' for i in range(1, 500)))
        self.assertTrue(extract_bytes(xlsx, "a.xlsx", None, 200)["truncated"])
        self.assertFalse(extract_bytes(xlsx, "a.xlsx", None, 20000)["truncated"])
        from dsbot.models import FileText

        self.assertTrue(FileText.from_dict({"text": "x", "truncated": True}).truncated)

    def test_corrupt_deflate_stream(self):
        import zlib

        body = "<w:p><w:r><w:t>content</w:t></w:r></w:p>" * 3000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "word/document.xml",
                f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
            )
        raw = bytearray(buf.getvalue())
        start = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")  # the compressed data starts right after the local file header
        for i in range(start + 2, start + 40):
            raw[i] ^= 0x5A
        with self.assertRaises(zlib.error):  # make sure the damage really is the "decompression error" kind
            zipfile.ZipFile(io.BytesIO(bytes(raw))).read("word/document.xml")
        self.assertEqual(extract_bytes(bytes(raw), "a.docx", None, 1000), {"error": "the file is corrupted and can't be opened"})


class _FakeProcess:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._out = (stdout, stderr)

    async def communicate(self, data=None):
        return self._out

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


class ChildStartup(unittest.IsolatedAsyncioTestCase):
    async def _read_with(self, process_factory, name="a.docx", raw=b"PK\x03\x04"):
        from unittest import mock

        async def fake_exec(*args, **kwargs):
            return process_factory()

        with mock.patch.object(files.asyncio, "create_subprocess_exec", fake_exec):
            return await read_file(raw, name, None, 100)

    async def test_child_that_cannot_start_is_a_temporary_problem(self):
        stderr = b"Traceback (most recent call last):\nModuleNotFoundError: No module named 'dsbot'\n"
        with self.assertLogs("dsbot.files", "ERROR"):
            result = await self._read_with(lambda: _FakeProcess(1, stderr=stderr))
        self.assertTrue(result.transient)
        self.assertIn("couldn't start", result.error)

    async def test_child_crash_is_a_problem_with_the_file(self):
        started = files.CHILD_READY.encode()  # it started fine and crashed while reading the file
        result = await self._read_with(lambda: _FakeProcess(-11, stdout=started, stderr=b"Segmentation fault"))
        self.assertFalse(result.transient)
        self.assertEqual(result.error, "the file is too complex or corrupted; nothing could be read")

    async def test_file_content_cannot_fake_a_startup_failure(self):
        started = files.CHILD_READY.encode()
        stderr = b"ModuleNotFoundError: PermissionError"  # e.g. text from a PDF that pypdf wrote into its log
        result = await self._read_with(lambda: _FakeProcess(-9, stdout=started, stderr=stderr))
        self.assertFalse(result.transient)

    async def _with_nobody(self, fake_exec):
        """Pretend the bot runs as root and drops to nobody, and read a file once with fake subprocesses. Returns (result, (user, is it a probe) for every subprocess started, whether privileges are still dropped)."""
        from unittest import mock

        calls = []

        async def recording_exec(*args, **kwargs):
            probe = bool(json.loads(args[3]).get("probe"))
            calls.append((kwargs.get("user"), probe))
            return await fake_exec(kwargs.get("user"), probe)

        with mock.patch.object(files, "_drop_privileges", True), mock.patch.object(files, "_child_user", lambda: dict(files._NOBODY) if files._drop_privileges else {}):
            with mock.patch.object(files.asyncio, "create_subprocess_exec", recording_exec):
                result = await read_file(b"PK", "a.docx", None, 100)
                dropping = files._drop_privileges
        return result, calls, dropping

    async def test_nobody_without_permission_stops_dropping_but_never_reads_the_file_as_root(self):
        denied = b"PermissionError: [Errno 13] Permission denied: '/app/dsbot/files.py'"

        async def fake_exec(user, probe):
            if user is not None:
                return _FakeProcess(1, stderr=denied)
            self.assertTrue(probe, "the file that triggered the failure must never be read as root instead")
            return _FakeProcess(0, stdout=files.CHILD_READY.encode())

        with self.assertLogs("dsbot.files", "WARNING"):
            result, calls, dropping = await self._with_nobody(fake_exec)
        self.assertTrue(result.transient)
        self.assertFalse(dropping)  # confirmed as a permission problem: use the current user from now on
        self.assertEqual(calls, [(files.CHILD_UID, False), (files.CHILD_UID, True), (None, True)])

    async def test_out_of_memory_does_not_stop_dropping(self):
        async def fake_exec(user, probe):
            raise OSError(errno.ENOMEM, "Cannot allocate memory")

        result, calls, dropping = await self._with_nobody(fake_exec)
        self.assertTrue(result.transient)
        self.assertTrue(dropping)
        self.assertEqual(calls, [(files.CHILD_UID, False)])  # no probing, let alone switching to root

    async def test_unconfirmed_permission_problem_keeps_dropping(self):
        async def fake_exec(user, probe):
            if probe:  # works when tried again: not a consistent permission problem
                return _FakeProcess(0, stdout=files.CHILD_READY.encode())
            return _FakeProcess(1, stderr=b"ModuleNotFoundError: No module named 'dsbot'")

        with self.assertLogs("dsbot.files", "ERROR"):
            result, calls, dropping = await self._with_nobody(fake_exec)
        self.assertTrue(dropping)
        self.assertEqual(calls, [(files.CHILD_UID, False), (files.CHILD_UID, True)])

    async def test_real_probe(self):
        self.assertEqual(await files._probe({}), "ok")

    def test_privilege_drop_decision(self):
        import os
        from unittest import mock

        if os.name != "posix" or os.geteuid() != 0:
            self.skipTest("privileges are only dropped when running as root")
        needs = [str(p) for p, _ in files._child_needs()]
        self.assertTrue(any(p.endswith("dsbot/files.py") for p in needs))
        self.assertTrue(any("pypdf" in p for p in needs))
        self.assertTrue(any(p.endswith("PIL") for p in needs))  # needed for the page images of scanned PDFs
        with mock.patch.object(files, "_drop_privileges", None), mock.patch.object(files, "_others_can_use", lambda p, read: True):
            self.assertEqual(files._child_user()["user"], files.CHILD_UID)
        hidden = lambda p, read: "charset_normalizer" not in str(p)  # noqa: E731  e.g. installed under /root/.local
        with mock.patch.object(files, "_drop_privileges", None), mock.patch.object(files, "_others_can_use", hidden):
            with self.assertLogs("dsbot.files", "WARNING"):
                self.assertEqual(files._child_user(), {})

    def test_child_finds_the_same_libraries(self):
        import sys

        paths = files._child_env()["PYTHONPATH"].split(os.pathsep)
        self.assertTrue(all(p in sys.path for p in paths))

    def test_no_privilege_drop_when_nobody_cannot_read_the_code(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        if os.name != "posix" or os.geteuid() != 0:
            self.skipTest("privileges are only dropped when running as root")
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)  # e.g. the project lives under /root
            with mock.patch.object(files, "PROJECT_ROOT", Path(d)), mock.patch.object(files, "_drop_privileges", None):
                with self.assertLogs("dsbot.files", "WARNING"):
                    self.assertEqual(files._child_user(), {})
                self.assertEqual(files._child_user(), {})  # checks and warns only once


if __name__ == "__main__":
    unittest.main()
