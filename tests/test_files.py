"""文件解析：用 python-docx / openpyxl / python-pptx / reportlab / Pillow 现场生成真实文件来测。

这些库只是测试用来造文件的，bot 本身不依赖它们（Office 文件用标准库解析）。没装的话对应的测试会跳过。
"""

import asyncio
import datetime
import errno
import io
import json
import os
import unittest
import zipfile

import helpers  # noqa: F401  (设置 sys.path)

from dsbot import files
from dsbot.files import classify, decode_text, extract_bytes, read_file


def need(module):
    try:
        return __import__(module)
    except ImportError:
        raise unittest.SkipTest(f"没装 {module}")


class Classify(unittest.TestCase):
    def test_kinds(self):
        cases = {
            ("report.pdf", None): "pdf",
            ("周报.docx", None): "docx",
            ("x", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"): "docx",
            ("budget.XLSX", None): "xlsx",
            ("deck.pptx", None): "pptx",
            ("main.ts", None): "text",  # TypeScript，不是视频
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
        self.assertIn(".docx", classify("old.doc", None)[1])  # 告诉对方怎么办

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
        text = "\n".join(f"第 {i} 行：" + "内容" * 20 for i in range(2000))
        r = extract_bytes(text.encode(), "long.txt", None, 5000)
        self.assertLessEqual(len(r["text"]), 5000)
        self.assertTrue(r["text"].endswith("内容"))  # 截在行尾
        self.assertIn("只读了前", r["detail"])

    def test_notebook_drops_outputs(self):
        nb = {"cells": [{"cell_type": "code", "source": ["print(1)\n"], "outputs": [{"data": {"image/png": "QUFB" * 5000}}]}]}
        r = extract_bytes(json.dumps(nb).encode(), "a.ipynb", None, 20000)
        self.assertEqual(r["text"], "[code]\nprint(1)")

    def test_binary_with_unknown_extension(self):
        self.assertIn("error", extract_bytes(b"\x00\x01\x02" * 100, "data.xyz", None, 1000))
        self.assertEqual(extract_bytes("没有扩展名".encode(), "README", None, 1000)["text"], "没有扩展名")

    def test_empty(self):
        self.assertEqual(extract_bytes(b"  \n ", "a.txt", None, 100)["detail"], "是空文件")


class Office(unittest.TestCase):
    def test_docx(self):
        docx = need("docx")
        d = docx.Document()
        d.add_heading("周报", 1)
        d.add_paragraph("本周完成：登录页。")
        d.add_paragraph("跟进合同", style="List Bullet")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "项目", "进度"
        t.cell(1, 0).text, t.cell(1, 1).text = "登录页", "90%"
        buf = io.BytesIO()
        d.save(buf)
        r = extract_bytes(buf.getvalue(), "周报.docx", None, 20000)
        self.assertEqual(r["kind"], "Word 文档")
        self.assertEqual(r["text"], "## 周报\n本周完成：登录页。\n• 跟进合同\n项目 | 进度\n登录页 | 90%")

    def test_xlsx(self):
        openpyxl = need("openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "开支"
        ws.append(["日期", "项目", "金额"])
        ws.append([datetime.date(2026, 10, 1), "火锅", 268.5])
        ws.append([datetime.datetime(2026, 10, 3, 19, 30), "电影", 120])
        ws["Z5"] = "远处"
        ws2 = wb.create_sheet("备注")
        ws2["A1"] = "AA 制"
        ws2["B2"] = True
        buf = io.BytesIO()
        wb.save(buf)
        r = extract_bytes(buf.getvalue(), "budget.xlsx", None, 20000)
        self.assertEqual(
            r["text"],
            "[工作表：开支]\n日期 | 项目 | 金额\n2026-10-01 | 火锅 | 268.5\n2026-10-03 19:30 | 电影 | 120\nZ5=远处\n\n"
            "[工作表：备注]\nAA 制\n | TRUE",
        )
        self.assertEqual(r["detail"], "共 2 个工作表")

    def test_xlsx_truncated(self):
        openpyxl = need("openpyxl")
        wb = openpyxl.Workbook()
        for i in range(500):
            wb.active.append([f"第{i}行", "x" * 30])
        buf = io.BytesIO()
        wb.save(buf)
        r = extract_bytes(buf.getvalue(), "big.xlsx", None, 2000)
        self.assertLessEqual(len(r["text"]), 2100)
        self.assertIn("只读了前面一部分", r["detail"])

    def test_pptx(self):
        pptx = need("pptx")
        prs = pptx.Presentation()
        s1 = prs.slides.add_slide(prs.slide_layouts[1])
        s1.shapes.title.text = "第一页"
        s1.placeholders[1].text = "要点一\n要点二"
        s2 = prs.slides.add_slide(prs.slide_layouts[5])
        s2.shapes.title.text = "总结"
        buf = io.BytesIO()
        prs.save(buf)
        r = extract_bytes(buf.getvalue(), "deck.pptx", None, 20000)
        self.assertEqual(r["text"], "[第 1 页]\n第一页\n要点一\n要点二\n\n[第 2 页]\n总结")
        self.assertEqual(r["detail"], "共 2 页")

    def test_zip_bomb_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", " " * (files.MAX_XML_BYTES + 1))
        self.assertLess(len(buf.getvalue()), 100_000)  # 压缩后很小，解压后超大
        self.assertEqual(extract_bytes(buf.getvalue(), "bomb.docx", None, 1000), {"error": "文件里的内容太大了，读不了"})

    def test_broken_files(self):
        self.assertEqual(extract_bytes(b"not a zip", "x.docx", None, 1000), {"error": "文件损坏了，打不开"})
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
        raw = self._pdf([["第一页：你好", "Hello PDF"], ["第二页"]])
        r = extract_bytes(raw, "a.pdf", None, 20000)
        self.assertEqual(r["text"], "[第 1 页]\n第一页：你好\nHello PDF\n\n[第 2 页]\n第二页")
        self.assertEqual((r["detail"], r["images"]), ("共 2 页", []))

    def test_truncated_pdf(self):
        raw = self._pdf([[f"第{p}页第{i}行" + "字" * 30 for i in range(30)] for p in range(10)])
        r = extract_bytes(raw, "a.pdf", None, 3000)
        self.assertLessEqual(len(r["text"]), 3000)
        self.assertRegex(r["detail"], r"^共 10 页，文字只读到第 \d 页$")

    def test_encrypted(self):
        from reportlab.lib.pdfencrypt import StandardEncryption

        locked = self._pdf([["秘密"]], encrypt=StandardEncryption("user-pw", "owner-pw"))
        self.assertEqual(extract_bytes(locked, "a.pdf", None, 1000), {"error": "PDF 有打开密码，读不了"})
        restricted = self._pdf([["只限制了打印"]], encrypt=StandardEncryption("", "owner-pw", canPrint=0))
        self.assertIn("只限制了打印", extract_bytes(restricted, "a.pdf", None, 1000)["text"])

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
        self.assertIn("可能是扫描件", r["detail"])

    def test_corrupt(self):
        self.assertEqual(extract_bytes(b"%PDF-1.4 junk" * 20, "a.pdf", None, 1000), {"error": "PDF 损坏了，打不开"})


class Subprocess(unittest.IsolatedAsyncioTestCase):
    async def test_office_and_pdf_go_through_child_process(self):
        docx = need("docx")
        d = docx.Document()
        d.add_paragraph("子进程里读的")
        buf = io.BytesIO()
        d.save(buf)
        result = await read_file(buf.getvalue(), "a.docx", None, 1000, timeout=30)
        self.assertEqual((result.kind, result.text, result.error), ("Word 文档", "子进程里读的", ""))

    async def test_timeout_kills_child(self):
        result = await read_file(b"%PDF-1.4" + b"0" * 1000, "slow.pdf", None, 1000, timeout=0.001)
        self.assertEqual(result.error, "文件太复杂，读了太久没读完")

    async def test_child_does_not_get_secrets(self):
        import os

        os.environ["DISCORD_TOKEN"] = "do-not-leak"
        try:
            self.assertNotIn("DISCORD_TOKEN", files._child_env())
        finally:
            del os.environ["DISCORD_TOKEN"]

    async def test_text_stays_in_process(self):
        result = await read_file("你好".encode(), "a.txt", None, 100)
        self.assertEqual((result.kind, result.text), ("文本", "你好"))

    async def test_unsupported(self):
        result = await read_file(b"x", "a.zip", None, 100)
        self.assertEqual(result.error, "压缩包，不会解压")



class ReviewFixes(unittest.TestCase):
    """第二轮评审发现的问题。"""

    def test_long_runs_of_spaces_are_fast(self):
        import time

        raw = (" " * 400_000 + "x\n") * 3
        start = time.time()
        r = extract_bytes(raw.encode(), "message.txt", None, 1200)
        self.assertLess(time.time() - start, 2.0)  # 以前的正则在这里是平方级，要几十分钟
        self.assertIn("只读了前", r["detail"])

    def test_big5_larger_than_detection_sample(self):
        line = "繁體中文的舊檔案，話說天下大勢，分久必合，合久必分。\n"
        for pad in range(4):  # 不管 20 万字节切在哪里都要对
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
        r["text"].encode("utf-8")  # 不会抛 UnicodeEncodeError
        json.dumps(r, ensure_ascii=False).encode("utf-8")
        from dsbot.models import FileText

        self.assertEqual(FileText.from_dict({"text": "a\udc80b"}).text, "a?b")

    def test_password_protected_office(self):
        r = extract_bytes(files.OLE_MAGIC + b"\x00" * 500, "secret.docx", None, 1000)
        self.assertIn("设了密码", r["error"])

    def test_docx_main_part_elsewhere(self):
        xml = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>在别处</w:t></w:r></w:p></w:body></w:document>"
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
        self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], "在别处")

    def test_docx_text_box_not_duplicated(self):
        ns = (
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        )
        box = "<w:txbxContent><w:p><w:r><w:t>文本框里的话</w:t></w:r></w:p></w:txbxContent>"
        xml = (
            f"<w:document {ns}><w:body><w:p><w:r><w:t>正文</w:t></w:r><w:r><mc:AlternateContent>"
            f"<mc:Choice>{box}</mc:Choice><mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p>"
            "</w:body></w:document>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], "正文\n文本框里的话")

    def test_pptx_soft_line_break(self):
        pptx = need("pptx")
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = "Price:\vfree"
        buf = io.BytesIO()
        prs.save(buf)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.pptx", None, 1000)["text"], "[第 1 页]\nPrice:\nfree")

    def _xlsx(self, rows_xml, styles=None, shared=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="表" sheetId="1" r:id="rId1"/></sheets></workbook>',
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
        row = '<row r="{0}"><c r="A{0}" t="inlineStr"><is><t>第{0}行的数据，有点长有点长有点长</t></is></c></row>'
        raw = self._xlsx("".join(row.format(i) for i in range(1, 600_001)))
        self.assertLess(len(raw), 20_000_000)
        r = extract_bytes(raw, "big.xlsx", None, 2000)
        self.assertTrue(r["text"].startswith("[工作表：表]\n第1行的数据"))
        self.assertIn("只读了前面一部分", r["detail"])
        self.assertLessEqual(len(r["text"]), 2000)

    def test_first_row_longer_than_budget(self):
        raw = self._xlsx('<row r="1"><c r="A1" t="inlineStr"><is><t>' + "长" * 2800 + "</t></is></c></row>")
        r = extract_bytes(raw, "a.xlsx", None, 2000)
        self.assertTrue(r["text"].startswith("[工作表：表]\n长长长"))
        self.assertLessEqual(len(r["text"]), 2000)
        self.assertIn("只读了前面一部分", r["detail"])

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
        self.assertEqual(extract_bytes(raw, "a.xlsx", None, 1000)["text"], "[工作表：表]\ninf | nan | inf | 25.6% | nan | 2025-12-09")

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
        self.assertEqual(extract_bytes(buf.getvalue(), "a.pdf", None, 20000)["detail"], "共 3 页")


class ChildIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_child_runs_as_nobody_when_root(self):
        import os
        import sys

        if os.name != "posix" or os.geteuid() != 0:
            self.skipTest("只有以 root 运行时才会降权")
        if not all(files._others_can_use(path, read=read) for path, read in files._child_needs()):
            # 和 _child_user() 一样：nobody 读不到 Python 或装的库（比如 venv 在 0700 的目录里）时 bot 本来就不降权
            self.skipTest("nobody 读不到这里的 Python 或库，bot 不会降权")
        kwargs = {"user": files.CHILD_UID, "group": files.CHILD_UID, "extra_groups": []}  # _child_user() 降权时给的参数
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
            result = await read_file("你好".encode(), "a.txt", None, 100)
        finally:
            files._run_child = original
        self.assertEqual((result.text, calls), ("你好", ["a.txt"]))


class ThirdRoundFixes(unittest.TestCase):
    """第二轮修复后复查发现的问题。"""

    def test_korean_and_japanese_legacy_encodings(self):
        cases = [
            ("안녕하세요. 오늘 회의는 오후 세 시에 시작합니다.\n자료는 미리 읽어 주세요.\n", "cp949"),
            ("대한민국(大韓民國)은 동아시아의 한반도 남부에 위치한 나라이다. 수도는 서울특별시이다.\n", "euc_kr"),
            ("ㅋㅋㅋ 진짜 웃기다\n오늘 저녁 뭐 먹을래?\n치킨 ㄱㄱ\n", "cp949"),
            ("こんにちは。今日の会議は午後三時から始まります。\n資料を事前に読んでおいてください。\n", "cp932"),
            ("東京都千代田区丸の内一丁目\n大阪府大阪市北区梅田三丁目\n", "cp932"),
            ("[00:12.34]君と初めて会った日\n[00:18.90]空はとても青かった\n", "euc_jp"),
            # 符号很多的游戏脚本：编码检测器直接放弃，要靠后备的逐个尝试
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
        raw = raw[:i] + b"\x8f\xfe" + raw[i + 2 :]  # 一个造字（Windows 自造字区），标准 Big5 表里没有
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
        box = "<w:txbxContent><w:p><w:r><w:t>框里</w:t></w:r></w:p></w:txbxContent>"
        cell = (
            "<w:tc><w:p><w:r><w:t>格子</w:t></w:r><w:r><mc:AlternateContent>"
            f"<mc:Choice>{box}</mc:Choice><mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p></w:tc>"
        )
        xml = (
            f"<w:document {ns}><w:body><w:tbl><w:tr>{cell}"
            "<w:tc><w:p><w:r><w:t>右边</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], "格子 框里 | 右边")

    def test_wrapped_rows_and_cells(self):
        ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

        def para(t):
            return f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"

        cases = {
            f"<w:tbl><w:tr><w:tc><w:customXml w:element='name'>{para('张三')}</w:customXml></w:tc><w:tc>{para('100')}</w:tc></w:tr></w:tbl>": "张三 | 100",
            f"<w:tbl><w:tr><w:sdt><w:sdtContent><w:tc>{para('张三')}</w:tc></w:sdtContent></w:sdt><w:tc>{para('100')}</w:tc></w:tr></w:tbl>": "张三 | 100",
            f"<w:tbl><w:tr><w:tc>{para('姓名')}</w:tc></w:tr><w:sdt><w:sdtContent><w:tr><w:tc>{para('张三')}</w:tc></w:tr></w:sdtContent></w:sdt></w:tbl>": "姓名\n张三",
            f"<w:customXml w:element='x'>{para('正文')}</w:customXml>": "正文",
        }
        for body, expected in cases.items():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>")
            self.assertEqual(extract_bytes(buf.getvalue(), "a.docx", None, 1000)["text"], expected)

    def test_readers_say_when_they_cut_the_file(self):
        self.assertTrue(extract_bytes(("字" * 3000).encode(), "a.txt", None, 1000)["truncated"])
        self.assertFalse(extract_bytes("短".encode(), "a.txt", None, 1000)["truncated"])
        xlsx = ReviewFixes()._xlsx("".join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>第{i}行</t></is></c></row>' for i in range(1, 500)))
        self.assertTrue(extract_bytes(xlsx, "a.xlsx", None, 200)["truncated"])
        self.assertFalse(extract_bytes(xlsx, "a.xlsx", None, 20000)["truncated"])
        from dsbot.models import FileText

        self.assertTrue(FileText.from_dict({"text": "x", "truncated": True}).truncated)

    def test_corrupt_deflate_stream(self):
        import zlib

        body = "<w:p><w:r><w:t>内容</w:t></w:r></w:p>" * 3000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "word/document.xml",
                f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
            )
        raw = bytearray(buf.getvalue())
        start = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")  # 本地文件头之后就是压缩数据
        for i in range(start + 2, start + 40):
            raw[i] ^= 0x5A
        with self.assertRaises(zlib.error):  # 确认造出来的确实是“解压出错”这种损坏
            zipfile.ZipFile(io.BytesIO(bytes(raw))).read("word/document.xml")
        self.assertEqual(extract_bytes(bytes(raw), "a.docx", None, 1000), {"error": "文件损坏了，打不开"})


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
        self.assertIn("启动不了", result.error)

    async def test_child_crash_is_a_problem_with_the_file(self):
        started = files.CHILD_READY.encode()  # 启动好了、读文件的时候崩的
        result = await self._read_with(lambda: _FakeProcess(-11, stdout=started, stderr=b"Segmentation fault"))
        self.assertFalse(result.transient)
        self.assertEqual(result.error, "文件太复杂或者损坏了，没读出来")

    async def test_file_content_cannot_fake_a_startup_failure(self):
        started = files.CHILD_READY.encode()
        stderr = b"ModuleNotFoundError: PermissionError"  # 比如 PDF 里的字被 pypdf 打进了日志
        result = await self._read_with(lambda: _FakeProcess(-9, stdout=started, stderr=stderr))
        self.assertFalse(result.transient)

    async def _with_nobody(self, fake_exec):
        """假装 bot 以 root 运行、会降成 nobody，用假的子进程跑一次读文件。返回 (结果, 每次开子进程的 (用户, 是不是探测))。"""
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
            self.assertTrue(probe, "触发失败的文件不能改用 root 去读")
            return _FakeProcess(0, stdout=files.CHILD_READY.encode())

        with self.assertLogs("dsbot.files", "WARNING"):
            result, calls, dropping = await self._with_nobody(fake_exec)
        self.assertTrue(result.transient)
        self.assertFalse(dropping)  # 确认是权限问题：以后改用当前用户
        self.assertEqual(calls, [(files.CHILD_UID, False), (files.CHILD_UID, True), (None, True)])

    async def test_out_of_memory_does_not_stop_dropping(self):
        async def fake_exec(user, probe):
            raise OSError(errno.ENOMEM, "Cannot allocate memory")

        result, calls, dropping = await self._with_nobody(fake_exec)
        self.assertTrue(result.transient)
        self.assertTrue(dropping)
        self.assertEqual(calls, [(files.CHILD_UID, False)])  # 不去探测，更不会改用 root

    async def test_unconfirmed_permission_problem_keeps_dropping(self):
        async def fake_exec(user, probe):
            if probe:  # 再试一次就好了：不是稳定的权限问题
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
            self.skipTest("只有以 root 运行时才会降权")
        needs = [str(p) for p, _ in files._child_needs()]
        self.assertTrue(any(p.endswith("dsbot/files.py") for p in needs))
        self.assertTrue(any("pypdf" in p for p in needs))
        self.assertTrue(any(p.endswith("PIL") for p in needs))  # 扫描版 PDF 的页面图要用
        with mock.patch.object(files, "_drop_privileges", None), mock.patch.object(files, "_others_can_use", lambda p, read: True):
            self.assertEqual(files._child_user()["user"], files.CHILD_UID)
        hidden = lambda p, read: "charset_normalizer" not in str(p)  # noqa: E731  比如装在 /root/.local 里
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
            self.skipTest("只有以 root 运行时才会降权")
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)  # 比如项目放在 /root 下
            with mock.patch.object(files, "PROJECT_ROOT", Path(d)), mock.patch.object(files, "_drop_privileges", None):
                with self.assertLogs("dsbot.files", "WARNING"):
                    self.assertEqual(files._child_user(), {})
                self.assertEqual(files._child_user(), {})  # 只检查、提醒一次


if __name__ == "__main__":
    unittest.main()
