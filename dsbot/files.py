"""读取群友上传的文件：文本 / 代码、PDF、Word、Excel、PowerPoint。

模型的接口只收图片（xAI 只收 JPG / PNG，看图前都会转成 JPEG），文档要先在这边转成文字。

所有文件都在子进程里解析：限内存、限 CPU，超时就杀掉，坏文件或恶意文件最多让这一次读取失败。
bot 以 root 运行时（Docker 里默认就是），子进程会降成 nobody 用户，读不到 bot 进程的环境变量（token、API key）。

单独运行（子进程入口）：python -m dsbot.files '{"filename": ..., "content_type": ..., "max_chars": ...}' < 文件
"""

from __future__ import annotations

import asyncio
import codecs
import errno
import io
import json
import logging
import math
import os
import posixpath
import re
import sys
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from .models import FileText
from .textutil import clean_text

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHILD_MEMORY_BYTES = 1024 * 1024 * 1024  # 子进程最多用 1 GB 内存
CHILD_CPU_SECONDS = 60
CHILD_UID = 65534  # nobody
MAX_XML_BYTES = 25 * 1024 * 1024  # Word / PPT 里单个 XML 解压后的上限（防 zip 炸弹）
MAX_SHEET_BYTES = 400 * 1024 * 1024  # Excel 工作表是边解压边解析、读够就停的，上限可以大一些
MAX_SHARED_STRINGS = 300_000
MAX_PDF_PAGES = 300
MAX_SHEETS = 10
MAX_COLUMNS = 50
SCAN_PAGES = 3  # 扫描版 PDF 最多附几页图
SNIFF_MAX_BYTES = 2 * 1024 * 1024  # 不认识的扩展名：不超过这个大小才下载下来看看是不是文本
DETECT_SAMPLE = 200_000
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 设了密码的 Office 文件、老格式 .doc/.xls/.ppt

TEXT_EXTS = set(
    """
    .txt .text .md .markdown .rst .log .csv .tsv .json .jsonl .ndjson .geojson .yaml .yml .toml .ini .cfg .conf
    .properties .env .xml .svg .html .htm .xhtml .css .scss .sass .less .js .mjs .cjs .ts .tsx .jsx .vue .svelte
    .py .pyw .pyi .ipynb .java .kt .kts .scala .groovy .gradle .go .rs .c .h .cpp .cc .cxx .hpp .hh .hxx .cs .fs
    .fsx .swift .m .mm .rb .php .pl .pm .lua .r .sql .sh .bash .zsh .fish .ps1 .psm1 .bat .cmd .dart .ex .exs
    .erl .hrl .hs .clj .cljs .edn .lisp .el .scm .vim .proto .graphql .gql .sol .asm .s .v .sv .vhd .vhdl .nim
    .zig .jl .ml .mli .tf .hcl .nix .cmake .mk .dockerfile .diff .patch .srt .ass .ssa .vtt .lrc .tex .bib .sty
    .cls .lock .gitignore .gitattributes .editorconfig .htaccess .reg .nfo
    """.split()
)
TEXT_NAMES = {"dockerfile", "makefile", "readme", "license", "changelog", "procfile", "gemfile", "rakefile", "jenkinsfile"}
TEXT_TYPES = {
    "application/json",
    "application/ld+json",
    "application/x-ndjson",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/x-python",
    "application/x-python-code",
    "application/sql",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/x-httpd-php",
    "application/csv",
}
DOCX_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
XLSX_TYPES = {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
PPTX_TYPES = {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}

_UNSUPPORTED_GROUPS = {
    "老版 Word 格式（.doc），另存为 .docx 或 PDF 再发": ".doc .dot .wps",
    "老版 Excel 格式（.xls），另存为 .xlsx 或 CSV 再发": ".xls .xlt .et",
    "老版 PPT 格式（.ppt），另存为 .pptx 或 PDF 再发": ".ppt .pot .dps",
    "RTF 格式读不好，另存为 .docx 或 PDF 再发": ".rtf",
    "压缩包，不会解压": ".zip .rar .7z .tar .gz .tgz .bz2 .xz .zst .lz .cab",
    "音频，听不了": ".mp3 .wav .flac .m4a .aac .ogg .oga .opus .wma .amr .mid .midi",
    "视频，看不了": ".mp4 .mov .mkv .avi .webm .flv .wmv .m4v .3gp .m2ts",
    "程序或安装包": ".exe .msi .apk .ipa .dmg .pkg .deb .rpm .iso .img .bin .dll .so .dylib .jar .class .pyc .o",
    "这种图片格式看不了，发 PNG 或 JPG 就行": ".heic .heif .avif .bmp .tif .tiff .psd .ai .raw .cr2 .nef .arw .dng .eps .xcf",
    "这种格式读不了": ".pages .numbers .key .odt .ods .odp .epub .mobi .azw3 .xps .djvu .sqlite .db .ttf .otf .woff .woff2",
}
UNSUPPORTED_EXTS = {ext: reason for reason, exts in _UNSUPPORTED_GROUPS.items() for ext in exts.split()}


class TooLarge(Exception):
    pass


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------


def classify(filename: str, content_type: str | None) -> tuple[str, str]:
    """返回 (类型, 读不了的原因)。类型：text / pdf / docx / xlsx / pptx / sniff（不认识，下载下来看看）/ unsupported"""
    name = (filename or "").lower()
    base = name.rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1] or (base if base.startswith(".") else "")
    ctype = (content_type or "").split(";")[0].strip().lower()

    if ext == ".pdf" or ctype == "application/pdf":
        return "pdf", ""
    if ext in (".docx", ".docm", ".dotx") or ctype in DOCX_TYPES:
        return "docx", ""
    if ext in (".xlsx", ".xlsm", ".xltx") or ctype in XLSX_TYPES:
        return "xlsx", ""
    if ext in (".pptx", ".pptm", ".ppsx", ".potx") or ctype in PPTX_TYPES:
        return "pptx", ""
    if ext in UNSUPPORTED_EXTS:
        return "unsupported", UNSUPPORTED_EXTS[ext]
    if ext in TEXT_EXTS or base in TEXT_NAMES or ctype.startswith("text/") or ctype in TEXT_TYPES:
        return "text", ""
    if ctype.startswith("audio/"):
        return "unsupported", "音频，听不了"
    if ctype.startswith("video/"):
        return "unsupported", "视频，看不了"
    if ctype.startswith("image/"):
        return "unsupported", "这种图片格式看不了，发 PNG 或 JPG 就行"
    return "sniff", ""


def max_bytes_for(kind: str, limit: int) -> int:
    return min(limit, SNIFF_MAX_BYTES) if kind == "sniff" else limit


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------

_UTF16_32 = ["utf_16", "utf_16_be", "utf_16_le", "utf_32", "utf_32_be", "utf_32_le"]
_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_KANA = re.compile(r"[\u3040-\u30ff]")  # 全角平假名、片假名
_HALF_KANA = re.compile(r"[\uff61-\uff9f]")  # 半角片假名
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 韩文里常用的音节（日常文本里九成以上的字都在这里面）。中文被误判成韩文编码时，解出来的音节大多不在里面
_FREQUENT_HANGUL = frozenset(
    "가각간갈감갔같개거건걸것게겠겨결경계고곤곧골곱곳공과곽관괄광괜괴굉교구국군권귀귄귤그극근글금긍기긴길김깃깅깊까깐깨꺼껏께꼭꼴꽃꿈꿔끔끝끼낌나낙난날남낮낯내낼냈냐냥너넌넓넘넣네넷녀녁년념녕노논놀놈농높놓"
    "뇌누눈눌눠뉴늄느는늘능늦니닉닌님닙다닥단닫달닮담답닷당닿대댁댄댓더덕던덜덤덥데뎌도독돈돌돕동됐되된될됨됩두둘둥뒤뒷듈드득든듣들듯등디딘딩딱땅때땐떠떡떤떨떻떼또똑똥뚜뜨뜰뜻띄라락란랄람랍랐랑래랜랩랫략량"
    "러런럼럽렀렇렉렌렘렛려렵렸령례로록론롤롭료루룹류르른를름릇릎리릭릴립링마막만많말맑맘맞매맥맨맵맷맹머먹먼멀멋메멘멤며면멸명모목몬몰몸못몽묘무묵문묻물뭇뭐뭔뭘뮤므미믹민밀밌밍및밑바박밖반받발밝밤밥방배백"
    "뱀뱅버벅번벌범법벗벙벚베벤벨벼벽변별병보복볶본볼봄봉봐봤뵙부북분불붉붐붓붙뷔뷰브븐블비빈빌빗빙빛빠빨빵뺨뻐뻔뻗뼈뽀뽑뿌뿐삐사삭산살삶삼삽상새색샌샘생샤서석섞선설섬섭성세센셀셈셋셔셜셨소속손솔송쇄쇠쇼숍"
    "수숙순술숨숫숲쉬쉰쉽슈슐스슨슬슴습슷승시식신실싫심십싱싶싸싼쌀쌍쌤써썸썼쏘쏟쑥쓰쓴씀씌씨씩아악안앉않알앓암압앗았앙앞애액앤앱야약얀양얘얜어억언얻얼엄업없엇었엉에엑엔엘엠여역연열엷염엽였영옆예옛오옥온올"
    "옮옳옵옷와완왔왕왜외왼요욕용우욱운울움웃웅워원월웠웨웬웹위윈윗윙유육윤으은을음읍응의이익인일읽잃임입잇있잉잊잎자작잔잖잘잠잡잤장재잭쟁저적전절젊점접정젖제젝젠젤져졌조족존졸좀좁종좋좌죄죠주죽준줄줍중줘"
    "쥐즈즉즐즘증지직진질짐집짓징짙짜짝짠짧째쩌쪽쫓쫙찌찍찜차착찬찮찰참창찾채책챙처척천철첨첫청체쳐초촌총최추축출춤충춰췄취츠측층치칙친칠침칭카칸칼캐캠캡커컨컬컴컵켓켜켰코콘콜콤콩쾌쿠퀘퀴큐크큰클큼키킨킬킹"
    "타탁탄탈탑탓탕태택탭탱터턱턴털텀테텍텐텔템토톡톤톱통퇴투툼퉁튀튜트특튼틀틈티틴팀팁팅파판팔팝패팩팬팸팽퍼퍽펀펑페펙펜펴편펼평폐포폭폰폴퐁표푸풀품풍퓨퓰프픈플피픽핀필핏핑하학한할함합항해핸햇했행향허헌헐"
    "험헤헬혀혁현혈협혔형혜호혹혼홀홍화확환활황회획효후훈훌훑훔훨휘휴흐흔흘흙흡흥희흰히힌힘"
)


_CHINESE = {"gb18030", "gbk", "gb2312", "hz", "big5", "big5hkscs", "cp950"}
_PUA = re.compile(r"[\ue000-\uf8ff\ufffd]")
_FAMILIES = {
    "ko": ["cp949", "euc_kr", "johab"],
    "ja-sjis": ["cp932", "shift_jis", "shift_jis_2004", "shift_jisx0213"],
    "ja-euc": ["euc_jp", "euc_jis_2004", "euc_jisx0213"],
}


def _language_of(encoding: str) -> str:
    e = encoding.lower().replace("-", "_")
    if e.startswith("iso2022"):
        return ""  # 7 位的转义编码，不会和 GBK / Big5 混淆
    if e in ("cp949", "euc_kr", "johab", "uhc"):
        return "ko"
    if e.startswith("euc_j"):
        return "ja-euc"
    if "jis" in e or e == "cp932":
        return "ja-sjis"
    return ""


def _plausible(text: str, language: str, *, min_hangul: int = 10) -> bool:
    """检测说是韩文 / 日文编码时，看解出来的东西像不像真的韩文 / 日文。

    中文文本（尤其是短的）常被误判成这几种编码，误判时解出来的东西有明显特征：
    - 当成韩文：解出来的谚文音节一大半是不常用的，或者汉字占了三成以上；
    - 当成 Shift_JIS：GBK / Big5 的首字节大多落在半角片假名区，解出来一大堆半角片假名；
    - 当成 EUC-JP：GB2312 的字会变成另一些汉字，但几乎没有假名。
    谚文太少（不到 10 个）的也不算，这个 bot 的用户主要说中文，拿不准就当中文。
    language 是 zh-gb / zh-big5 时检查像不像中文（只在检测器没给出结果时用）。
    """
    han = len(_HAN.findall(text))
    if language == "ko":
        hangul = _HANGUL.findall(text)
        if not hangul:
            return False
        frequent = sum(1 for c in hangul if c in _FREQUENT_HANGUL)
        if len(hangul) < min_hangul:
            # 字少的时候要求更严：没有汉字，而且每个音节都是常用的
            return len(hangul) >= 4 and han == 0 and frequent == len(hangul)
        return frequent >= len(hangul) * 0.75 and han <= len(hangul) * 0.3
    kana = len(_KANA.findall(text))
    if language == "ja-euc":
        return kana > 0
    if language == "ja-sjis":
        half = len(_HALF_KANA.findall(text))
        return kana + han > 0 and half <= (kana + han + half) * 0.3
    # 中文（只在检测器放弃时用来挑编码）：绝大部分字是常用字，几乎没有假名、谚文、私用区字符
    chars = _HAN.findall(text)
    if not chars:
        return False
    junk = kana + len(_HANGUL.findall(text)) + len(_HALF_KANA.findall(text)) + len(_PUA.findall(text))
    common = sum(1 for c in chars if _is_common_hanzi(c, language))
    return junk <= len(chars) * 0.05 and common >= len(chars) * 0.6


def _is_common_hanzi(char: str, language: str) -> bool:
    """GB2312 一级字（3755 个常用简体字）/ Big5 常用字（5401 个）。"""
    try:
        if language == "zh-gb":
            return 0xB0 <= char.encode("gb2312")[0] <= 0xD7
        return 0xA440 <= int.from_bytes(char.encode("big5"), "big") <= 0xC67E
    except UnicodeEncodeError:
        return False


def _sample(raw: bytes) -> bytes:
    """检测编码用的开头一段。截在换行处，别把一个双字节字切成两半，否则检测会失败。"""
    if len(raw) <= DETECT_SAMPLE:
        return raw
    sample = raw[:DETECT_SAMPLE]
    newline = sample.rfind(b"\n")
    return sample[: newline + 1] if newline > 0 else sample


def _detect(sample: bytes) -> str | None:
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        from_bytes = None
    try:
        return _choose_encoding(sample, from_bytes)
    except Exception:
        return None


def _cjk_count(text: str) -> int:
    return len(_HAN.findall(text)) + len(_KANA.findall(text)) + len(_HANGUL.findall(text))


# 日文里很少见的假名：小写的あいうえお、ゎゐゑゔゕゖ、单独的浊点半浊点等。Big5 的常用字当成 EUC-JP 解时常冒出这些
_RARE_KANA = re.compile(r"[\u3041\u3043\u3045\u3047\u3049\u308e\u3090\u3091\u3094-\u3096\u3099-\u309c\u30ee\u30f0\u30f1\u30f7-\u30fa]")


def _clearly_japanese(text: str) -> bool:
    kana = len(_KANA.findall(text))
    return kana >= 3 and kana >= _cjk_count(text) * 0.3 and not _RARE_KANA.search(text)


def _lenient(sample: bytes, encoding: str) -> str | None:
    """宽松地解码：偶尔几个坏字节（比如 Big5 里的造字）不要紧，坏得多就算不是这个编码。"""
    text = sample.decode(encoding, "replace")
    bad = text.count("\ufffd")
    non_ascii = len(text) - len(text.encode("ascii", "ignore"))
    return None if bad > max(2, non_ascii * 0.01) else text


def _choose_encoding(sample: bytes, from_bytes: Any) -> str | None:
    """按 charset_normalizer 的排名挑编码，但不轻信“韩文 / 日文”的猜测（短的中文常被误判成它们）。"""
    excluded = list(_UTF16_32)
    trusted = True  # 还没否掉过韩文 / 日文的猜测时，检测结果照单全收
    first_rejected: str | None = None  # 检测器最先猜的、被否掉的韩文 / 日文编码
    weak_guess: str | None = None  # 像韩文，只是字太少、拿不准
    chinese: list[str | None] = []

    def chinese_reading() -> str | None:
        """按中文（GB18030 / Big5）能不能读通，检测器说了算。只问一次。"""
        if not chinese:
            best = from_bytes(sample, cp_isolation=["gb18030", "big5"]).best()
            chinese.append(best.encoding if best is not None else None)
        return chinese[0]

    if from_bytes is not None:
        for _ in range(3):
            results = from_bytes(sample, cp_exclusion=excluded)
            if not results:
                break
            for match in results:  # 按可信度排好的
                encoding = match.encoding
                language = _language_of(encoding)
                if not language:
                    if trusted:
                        return encoding
                    if encoding in _CHINESE and chinese_reading():
                        return chinese_reading()
                    continue  # 否掉韩文 / 日文之后排在后面的其他编码不可信（按中文读不通时的 Big5-HKSCS、阿拉伯文之类）
                text = sample.decode(encoding, "replace")
                if _plausible(text, language):
                    if (
                        language == "ja-euc"
                        and _cjk_count(text) < SHORT_TEXT
                        and not _clearly_japanese(text)
                        and chinese_reading()
                    ):
                        return chinese_reading()  # 短的 EUC-JP 和 Big5 光看字很难分，这里的用户多半说中文
                    return encoding
                if weak_guess is None and language == "ko" and _plausible(text, language, min_hangul=1):
                    weak_guess = encoding
                first_rejected = first_rejected or encoding
                trusted = False
                excluded += [e for e in _FAMILIES[language] if e not in excluded]
            if chinese_reading():
                return chinese_reading()  # 说是韩文 / 日文但解出来不像，按中文又读得通：多半是中文
    # 检测器没给出能用的结果（比如符号很多的日文游戏脚本、有造字的 Big5 文件）：自己挨个试
    for encoding, language in _FALLBACKS:
        if language.startswith("zh") and first_rejected:
            continue  # 检测器已经认定按中文读不通
        text = _lenient(sample, encoding)
        if text is not None and _plausible(text, language):
            return encoding
    # 还是没有：相信检测器最初的猜测（比如半角片假名很多的日文表格、汉字很多的韩文）
    return weak_guess or first_rejected


SHORT_TEXT = 40  # 中日韩文字少于这么多算短文本
# 检测器放弃时挨个试的顺序。韩文的条件最严（中文几乎过不了）放最前；
# EUC-JP 的假名在 Big5 里正好是一批常用字，所以 Big5 放在日文后面（Big5 解成 EUC-JP 会有大量坏字节，不会误认）
_FALLBACKS = [
    ("cp949", "ko"),
    ("gb18030", "zh-gb"),
    ("cp932", "ja-sjis"),
    ("euc_jp", "ja-euc"),
    ("big5", "zh-big5"),
]


def decode_text(raw: bytes) -> str | None:
    """解码文本文件；看起来是二进制时返回 None。UTF-8 优先，老文件兼容 GBK/GB18030、Big5，以及日文、韩文的老编码。"""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8", "replace")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):  # 要先于 UTF-16 判断，它们的 BOM 开头一样
        return raw.decode("utf-32", "replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", "replace")
    if b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    encoding = _detect(_sample(raw))
    if encoding:
        try:
            return raw.decode(encoding, "replace")
        except LookupError:
            pass
    for encoding in ("gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def tidy(text: str) -> str:
    """统一换行、去掉行尾空白、合并多余空行。全是线性操作（用正则去行尾空白，碰上一长串空格会很慢）。"""
    text = clean_text(text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cut(text: str, max_chars: int) -> tuple[str, bool]:
    """截到 max_chars，尽量截在换行处。"""
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    newline = head.rfind("\n")
    if newline > max_chars * 0.9:
        head = head[:newline]
    return head.rstrip(), True


def _notebook(text: str) -> str:
    """Jupyter 笔记本：只要单元格的代码和文字，不要输出（输出里常有大段 base64 图片）。"""
    try:
        cells = json.loads(text).get("cells") or []
    except (ValueError, AttributeError):
        return text
    out = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        source = cell.get("source") or ""
        source = "".join(str(s) for s in source) if isinstance(source, list) else str(source)
        out.append(f"[{cell.get('cell_type', 'cell')}]\n{source}")
    return "\n\n".join(out)


def _text(raw: bytes, filename: str, max_chars: int) -> dict[str, Any]:
    text = decode_text(raw)
    if text is None:
        return {"error": "不是文本文件，这种格式读不了"}
    if filename.lower().endswith(".ipynb"):
        text = _notebook(text)
    text = text.lstrip()  # 开头一大段空白不算内容，也别占了下面那个窗口
    window = max_chars * 2 + 10_000
    work = tidy(text[:window])  # 只整理会用到的那一段
    if not work:
        return {"kind": "文本", "detail": "是空文件"}
    body, truncated = cut(work, max_chars)
    truncated = truncated or len(text) > window
    detail = f"只读了前 {len(body)} 字（全文约 {len(text)} 字）" if truncated else ""
    return {"kind": "文本", "text": body, "detail": detail, "truncated": truncated}


# ---------------------------------------------------------------------------
# PDF（pypdf）
# ---------------------------------------------------------------------------


def _pdf(raw: bytes, max_chars: int) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"error": "bot 这边没装好 pypdf，暂时读不了 PDF", "transient": True}

    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        # 很多 PDF 只限制了打印/复制，没有打开密码，用空密码就能解开
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:
            if type(exc).__name__ == "DependencyError":
                return {"error": "加密的 PDF，bot 少装了解密组件（pypdf[crypto]）"}
            unlocked = 0
        if not unlocked:
            return {"error": "PDF 有打开密码，读不了"}

    total = len(reader.pages)
    if not total:
        return {"error": "是空的 PDF"}
    parts: list[str] = []
    used = 0
    real_chars = 0
    done = 0
    for index in range(min(total, MAX_PDF_PAGES)):
        if used >= max_chars:
            break
        done = index + 1
        try:
            page_text = tidy(reader.pages[index].extract_text() or "")
        except Exception:
            page_text = ""
        if page_text:
            block = f"[第 {index + 1} 页]\n{page_text}"
            parts.append(block)
            used += len(block) + 2
            real_chars += len(re.sub(r"\s", "", page_text))

    images: list[str] = []
    if real_chars < 20 * max(1, min(done, 3)):
        # 文字很少：多半是扫描件或照片做成的 PDF，附上前几页的图
        images = _pdf_page_images(reader, min(total, SCAN_PAGES))
    body, truncated = cut("\n\n".join(parts), max_chars)
    if not body and not images:
        return {"error": f"PDF（共 {total} 页）里没有能读的文字"}
    detail = f"共 {total} 页"
    if body and truncated:
        pages = re.findall(r"^\[第 (\d+) 页\]$", body, re.M)
        detail += f"，文字只读到第 {pages[-1] if pages else 1} 页"
    elif body and done < total:
        detail += f"，文字只读到第 {done} 页"
    if images:
        detail += f"，文字很少（可能是扫描件），附上前 {len(images)} 页的图"
    return {"kind": "PDF", "text": body, "detail": detail, "images": images, "truncated": truncated or done < total}


def _pdf_page_images(reader: Any, pages: int) -> list[str]:
    from .images import to_data_uri

    out = []
    for index in range(pages):
        best = b""
        try:
            for image in reader.pages[index].images:
                data = image.data
                if len(data) > len(best):
                    best = data
        except Exception:
            continue
        if best:
            uri = to_data_uri(best, max_side=1400)
            if uri:
                out.append(uri)
    return out


# ---------------------------------------------------------------------------
# Office（docx / xlsx / pptx 都是 zip + XML，用标准库直接读）
# ---------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:
        raise TooLarge(name)
    return ET.fromstring(z.read(info))


def _part_path(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(base_dir, target))


def _rels(z: zipfile.ZipFile, rels_path: str, base_dir: str) -> dict[str, str]:
    root = _xml(z, rels_path)
    if root is None:
        return {}
    out = {}
    for rel in root.iter(PKG + "Relationship"):
        target = rel.get("Target") or ""
        if rel.get("TargetMode") == "External" or not target:
            continue
        out[rel.get("Id") or ""] = _part_path(base_dir, target)
    return out


def _main_part(z: zipfile.ZipFile, default: str) -> str:
    """文档主体一般在固定位置，个别软件会放在别处（比如 word/document2.xml），以 _rels/.rels 为准。"""
    try:
        root = _xml(z, "_rels/.rels")
    except (ET.ParseError, TooLarge):
        return default
    if root is not None:
        for rel in root.iter(PKG + "Relationship"):
            target = rel.get("Target") or ""
            if (rel.get("Type") or "").endswith("/officeDocument") and target:
                return _part_path("", target)
    return default


def _docx_para(p: ET.Element) -> list[str]:
    """一个段落 → [段落文字, 段落里文本框的各行…]。"""
    out: list[str] = []
    extra: list[str] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            tag = child.tag
            if tag == MC + "Fallback":
                continue  # 文本框等内容在 Choice 和 Fallback 里各存一份，只读一份
            if tag == W + "t":
                out.append(child.text or "")
            elif tag == W + "tab":
                out.append("\t")
            elif tag in (W + "br", W + "cr"):
                out.append("\n")
            elif tag == W + "txbxContent":
                _docx_block(child, extra)  # 文本框里的段落单独成行
            else:
                walk(child)

    walk(p)
    text = "".join(out)
    ppr = p.find(W + "pPr")
    if text.strip() and ppr is not None:
        style_node = ppr.find(W + "pStyle")
        style = ((style_node.get(W + "val") if style_node is not None else "") or "").lower()
        if style.startswith(("heading", "title")):
            text = "## " + text  # 标题
        elif ppr.find(W + "numPr") is not None or "list" in style:
            text = "• " + text  # 列表项
    return [text] + extra


def _wrapped(child: ET.Element) -> ET.Element | None:
    """内容控件（sdt）、customXml、兼容性包装：返回里面真正的内容。"""
    if child.tag == W + "sdt":
        return child.find(W + "sdtContent")
    if child.tag == W + "customXml":
        return child
    if child.tag == MC + "AlternateContent":
        return child.find(MC + "Choice")  # Fallback 是同一份内容，只读一份
    return None


def _children(container: ET.Element, tag: str) -> Iterator[ET.Element]:
    """容器里的某种元素（表格的行、行里的单元格），穿过各种包装。"""
    for child in container:
        if child.tag == tag:
            yield child
        else:
            inner = _wrapped(child)
            if inner is not None:
                yield from _children(inner, tag)


def _docx_block(container: ET.Element, lines: list[str]) -> None:
    for child in container:
        if child.tag == W + "p":
            lines += _docx_para(child)
        elif child.tag == W + "tbl":
            for row in _children(child, W + "tr"):
                cells = []
                for cell in _children(row, W + "tc"):
                    cell_lines: list[str] = []
                    _docx_block(cell, cell_lines)  # 只走直接子元素，文本框不会被读好几遍
                    cells.append(" ".join(x.strip() for x in cell_lines if x.strip()))
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
        else:
            inner = _wrapped(child)
            if inner is not None:
                _docx_block(inner, lines)


def _docx(raw: bytes, max_chars: int) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = _xml(z, _main_part(z, "word/document.xml"))
    body = root.find(W + "body") if root is not None else None
    if body is None:
        return {"error": "不是有效的 Word 文件"}
    lines: list[str] = []
    _docx_block(body, lines)
    text = tidy("\n".join(lines))
    if not text:
        return {"kind": "Word 文档", "detail": "里面没有文字（可能只有图片）"}
    out, truncated = cut(text, max_chars)
    detail = f"只读了前 {len(out)} 字（全文约 {len(text)} 字）" if truncated else ""
    return {"kind": "Word 文档", "text": out, "detail": detail, "truncated": truncated}


def _pptx_slides(z: zipfile.ZipFile) -> list[str]:
    try:
        pres = _xml(z, "ppt/presentation.xml")
        targets = _rels(z, "ppt/_rels/presentation.xml.rels", "ppt")
        if pres is not None:
            names = [targets.get(s.get(R + "id") or "") for s in pres.iter(P + "sldId")]
            names = [n for n in names if n]
            if names:
                return names
    except ET.ParseError:
        pass
    found = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return sorted(found, key=lambda n: int(re.findall(r"\d+", n)[-1]))


def _pptx_para(p: ET.Element) -> str:
    out = []
    for child in p:
        if child.tag in (A + "r", A + "fld"):
            t = child.find(A + "t")
            out.append((t.text or "") if t is not None else "")
        elif child.tag == A + "br":
            out.append("\n")  # 段落里的软换行
    return "".join(out).strip()


def _pptx(raw: bytes, max_chars: int) -> dict[str, Any]:
    parts: list[str] = []
    used = 0
    done = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        slides = _pptx_slides(z)
        for number, name in enumerate(slides, 1):
            if used >= max_chars:
                break
            done = number
            root = _xml(z, name)
            if root is None:
                continue
            paras = [t for t in (_pptx_para(p) for p in root.iter(A + "p")) if t]
            if paras:
                block = f"[第 {number} 页]\n" + "\n".join(paras)
                parts.append(block)
                used += len(block) + 2
    if not slides:
        return {"error": "不是有效的 PPT 文件"}
    if not parts:
        return {"kind": "PPT", "detail": f"共 {len(slides)} 页，里面没有文字（可能只有图片）"}
    body, truncated = cut(tidy("\n\n".join(parts)), max_chars)
    detail = f"共 {len(slides)} 页"
    if truncated:
        pages = re.findall(r"^\[第 (\d+) 页\]$", body, re.M)
        detail += f"，只读到第 {pages[-1] if pages else 1} 页"
    elif done < len(slides):
        detail += f"，只读到第 {done} 页"
    return {"kind": "PPT", "text": body, "detail": detail, "truncated": truncated or done < len(slides)}


_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))
_BUILTIN_PERCENT_FORMATS = {9, 10}


def _format_kind(code: str) -> str | None:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)  # 去掉引号里的文字、[颜色]、转义字符
    if "%" in code:
        return "percent"
    if re.search(r"[yYdD]|[hH].*[mM]|[mM].*[sS]", code):
        return "date"
    return None


def _cell_styles(z: zipfile.ZipFile) -> dict[int, str]:
    """单元格样式编号 → "date" / "percent"。"""
    root = _xml(z, "xl/styles.xml")
    if root is None:
        return {}
    custom = {}
    for fmt in root.iter(S + "numFmt"):
        try:
            custom[int(fmt.get("numFmtId") or "")] = fmt.get("formatCode") or ""
        except ValueError:
            continue
    xfs = root.find(S + "cellXfs")
    out = {}
    for index, xf in enumerate(xfs.findall(S + "xf") if xfs is not None else []):
        try:
            fmt_id = int(xf.get("numFmtId") or "0")
        except ValueError:
            continue
        if fmt_id in _BUILTIN_DATE_FORMATS:
            out[index] = "date"
        elif fmt_id in _BUILTIN_PERCENT_FORMATS:
            out[index] = "percent"
        elif fmt_id in custom:
            kind = _format_kind(custom[fmt_id])
            if kind:
                out[index] = kind
    return out


def _number(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


def _excel_date(serial: float, date1904: bool) -> str:
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    try:
        moment = base + timedelta(days=serial)
    except (OverflowError, ValueError):
        return _number(serial)
    if serial < 1:
        return moment.strftime("%H:%M")
    if serial == int(serial):
        return moment.date().isoformat()
    return moment.strftime("%Y-%m-%d %H:%M")


def _si_text(si: ET.Element) -> str:
    parts = []
    for child in si:
        if child.tag == S + "t":
            parts.append(child.text or "")
        elif child.tag == S + "r":
            t = child.find(S + "t")
            parts.append((t.text or "") if t is not None else "")
    return "".join(parts)  # rPh（注音）不要


def _column(ref: str | None) -> int | None:
    if not ref:
        return None
    letters = re.match(r"[A-Z]+", ref.upper())
    if not letters:
        return None
    index = 0
    for ch in letters.group(0):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _row_line(cells: dict[int, str], row_number: str) -> str:
    """一行表格转成文字。中间空格太多的稀疏行改成「Z6=值」，免得一长串空列。"""
    filled = [(i, v.replace("\n", " ")) for i, v in sorted(cells.items()) if v]
    width = max(cells) + 1
    if width - len(filled) > 8 and width - len(filled) > 2 * len(filled):
        return "; ".join(f"{_column_name(i)}{row_number}={v}" for i, v in filled)
    values = [cells.get(i, "").replace("\n", " ") for i in range(width)]
    while values and not values[-1]:
        values.pop()
    return " | ".join(values)


def _cell(c: ET.Element, shared: list[str], styles: dict[int, str], date1904: bool) -> str:
    kind = c.get("t")
    v = c.find(S + "v")
    text = v.text if v is not None and v.text is not None else ""
    if kind == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return ""
    if kind == "inlineStr":
        node = c.find(S + "is")
        return _si_text(node) if node is not None else ""
    if kind == "b":
        return "TRUE" if text == "1" else "FALSE"
    if kind in ("str", "e", "d") or not text:
        return text
    try:
        value = float(text)
    except ValueError:
        return text
    try:
        style = styles.get(int(c.get("s") or "0"))
    except ValueError:
        style = None
    if style == "date" and math.isfinite(value):
        return _excel_date(value, date1904)
    if style == "percent" and math.isfinite(value):
        return _number(value * 100) + "%"
    return _number(value)


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        info = z.getinfo("xl/sharedStrings.xml")
    except KeyError:
        return []
    if info.file_size > MAX_SHEET_BYTES:
        raise TooLarge(info.filename)
    out: list[str] = []
    with z.open(info) as fh:
        for _, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag == S + "si":
                out.append(_si_text(elem))
                elem.clear()
                if len(out) >= MAX_SHARED_STRINGS:
                    break  # 前面几行用到的字符串都排在前面，够用了
    return out


def _sheet_rows(z: zipfile.ZipFile, path: str) -> Iterator[ET.Element]:
    """边解压边解析工作表，一行一行给出来；读够了调用方就停，大表也不用整个读进内存。"""
    try:
        info = z.getinfo(path)
    except KeyError:
        return
    if info.file_size > MAX_SHEET_BYTES:
        raise TooLarge(path)
    with z.open(info) as fh:
        for _, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag == S + "row":
                yield elem
                elem.clear()


def _xlsx(raw: bytes, max_chars: int) -> dict[str, Any]:
    parts: list[str] = []
    used = 0
    truncated = False
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        workbook_path = _main_part(z, "xl/workbook.xml")
        workbook = _xml(z, workbook_path)
        if workbook is None:
            return {"error": "不是有效的 Excel 文件"}
        base = posixpath.dirname(workbook_path)
        pr = workbook.find(S + "workbookPr")
        date1904 = pr is not None and (pr.get("date1904") or "").lower() in ("1", "true")
        rels_path = posixpath.join(base, "_rels", posixpath.basename(workbook_path) + ".rels")
        targets = _rels(z, rels_path, base)
        sheets = [(s.get("name") or "?", targets.get(s.get(R + "id") or "")) for s in workbook.iter(S + "sheet")]
        sheets = [(name, path) for name, path in sheets if path]
        shared = _shared_strings(z)
        styles = _cell_styles(z)

        for name, path in sheets[:MAX_SHEETS]:
            header = f"[工作表：{name}]"
            budget = max_chars - used - len(header) - 1
            if budget < 20:
                truncated = True
                break
            rows: list[str] = []
            for row_index, row in enumerate(_sheet_rows(z, path), 1):
                number = row.get("r") or str(row_index)
                cells: dict[int, str] = {}
                for c in row.findall(S + "c"):
                    col = _column(c.get("r"))
                    col = len(cells) if col is None else col
                    if col < MAX_COLUMNS:
                        cells[col] = _cell(c, shared, styles, date1904)
                if not any(cells.values()):
                    continue
                line = _row_line(cells, number)
                if len(line) + 1 > budget:
                    if not rows and not parts:
                        rows.append(line[:budget])  # 第一行就超长：截断，总比说“表是空的”强
                    truncated = True
                    break
                rows.append(line)
                budget -= len(line) + 1
            if rows:
                block = header + "\n" + "\n".join(rows)
                parts.append(block)
                used += len(block) + 2
            if truncated:
                break
    if not parts:
        return {"kind": "Excel 表格", "detail": f"共 {len(sheets)} 个工作表，都是空的"}
    truncated = truncated or len(sheets) > MAX_SHEETS
    detail = f"共 {len(sheets)} 个工作表" + ("，太长了，只读了前面一部分" if truncated else "")
    return {"kind": "Excel 表格", "text": tidy("\n\n".join(parts)), "detail": detail, "truncated": truncated}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _clean_result(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("kind", "text", "detail", "error"):
        if isinstance(result.get(key), str):
            result[key] = clean_text(result[key])
    return result


def extract_bytes(raw: bytes, filename: str, content_type: str | None, max_chars: int) -> dict[str, Any]:
    """纯函数：把文件内容转成文字。返回可以转成 FileText 的 dict。"""
    kind, reason = classify(filename, content_type)
    if kind == "unsupported":
        return {"error": reason}
    if kind in ("docx", "xlsx", "pptx") and raw.startswith(OLE_MAGIC):
        return {"error": "这个 Office 文件设了密码（或者其实是老格式），读不了"}
    readers = {"text": _text, "sniff": _text, "pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "pptx": _pptx}
    try:
        if kind in ("text", "sniff"):
            return _clean_result(_text(raw, filename, max_chars))
        return _clean_result(readers[kind](raw, max_chars))
    except MemoryError:
        return {"error": "文件太复杂，读的时候内存不够"}
    except TooLarge:
        return {"error": "文件里的内容太大了，读不了"}
    except (zipfile.BadZipFile, zlib.error, EOFError):
        return {"error": "文件损坏了，打不开"}
    except Exception as exc:
        name = type(exc).__name__
        if name in ("PdfReadError", "PdfStreamError", "EmptyFileError"):
            return {"error": "PDF 损坏了，打不开"}
        if isinstance(exc, (ET.ParseError, ValueError, KeyError)):
            return {"error": "文件损坏了或者格式不对，打不开"}
        return {"error": f"解析失败（{name}）"}


def _import_paths() -> list[str]:
    return [p for p in sys.path if p and os.path.exists(p)]


def _child_env() -> dict[str, str]:
    """子进程只拿最少的环境变量：解析不受信任的文件时，碰不到 token 和 API key。

    PYTHONPATH 设成和 bot 一样的搜索路径：降成 nobody 后，装在 root 用户目录里的库也照样找得到。
    """
    keep = ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
    env["PYTHONPATH"] = os.pathsep.join(_import_paths())
    return env


def _others_can_use(path: str | Path, *, read: bool) -> bool:
    """其他用户（nobody）能不能用到这个路径：一路上的目录都能进，本身能读/能执行。"""
    try:
        target = Path(path).resolve()
        mode = target.stat().st_mode
        need = 0o005 if target.is_dir() else (0o004 if read else 0o001)
        if mode & need != need:
            return False
        for parent in target.parents:
            if parent.stat().st_mode & 0o001 == 0:
                return False
        return True
    except OSError:
        return False


_drop_privileges: bool | None = None
_NOBODY = {"user": CHILD_UID, "group": CHILD_UID, "extra_groups": []}
_CHILD_LIBRARIES = ("pypdf", "charset_normalizer", "cryptography", "PIL")  # 子进程会用到的第三方库


def _child_needs() -> list[tuple[str | Path, bool]]:
    """子进程要用到的东西：(路径, 是要读还是要执行)。"""
    import importlib.util

    stdlib = Path(os.__file__).parent
    needs: list[tuple[str | Path, bool]] = [(sys.executable, False), (stdlib, True), (PROJECT_ROOT, True)]
    if (stdlib / "lib-dynload").exists():
        needs.append((stdlib / "lib-dynload", True))
    needs += [(f, True) for f in (PROJECT_ROOT / "dsbot").glob("*.py")]
    for name in _CHILD_LIBRARIES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is not None and spec.origin:
            needs.append((Path(spec.origin).parent, True))
    return needs


def _child_user() -> dict[str, Any]:
    """bot 以 root 运行时（Docker 里默认如此），子进程降成 nobody，读不到 bot 进程的 /proc/…/environ（token、API key）。

    只有 nobody 读得到 Python、bot 的代码和装的库时才降权；否则（比如库装在 /root 下）照常运行并提醒一次。
    权限位看着没问题、实际上 nobody 还是启动不了（比如 ACL）时，由 _check_dropping 确认后改为不降权。
    """
    global _drop_privileges
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return {}
    if _drop_privileges is None:
        _drop_privileges = all(_others_can_use(p, read=r) for p, r in _child_needs())
        if not _drop_privileges:
            log.warning(
                "解析文件的子进程没法降成 nobody 运行（Python、bot 的代码或者装的库所在的目录其他用户读不到），"
                "会以当前用户运行。用 Docker 部署就没有这个问题。"
            )
    return dict(_NOBODY) if _drop_privileges else {}


CHILD_READY = "dsbot-child-ready\n"  # 子进程把要用的库都导入好、开始读文件之前先打印这一行
_PRELOAD = ("pypdf", "charset_normalizer", "PIL.Image")  # 子进程启动时就导入（装了的话），导入不了算启动失败
_preload: list[str] | None = None
_checking_drop = False


def _preload_libraries() -> list[str]:
    global _preload
    if _preload is None:
        import importlib.util

        found = []
        for name in _PRELOAD:
            try:
                if importlib.util.find_spec(name.split(".")[0]) is not None:
                    found.append(name)
            except (ImportError, ValueError):
                pass
        _preload = found
    return _preload


def _looks_like_permission_problem(stderr: bytes) -> bool:
    """子进程在读文件之前就退出了：看是不是没权限读代码或库（这时还没碰文件内容，输出是可信的）。"""
    text = stderr.decode("utf-8", "replace")
    return any(s in text for s in ("PermissionError", "Permission denied", "ModuleNotFoundError", "ImportError"))


def _child_args(**extra: Any) -> str:
    return json.dumps({**extra, "libs": _preload_libraries()})  # 纯 ASCII


async def _spawn(args: str, user: dict[str, Any], *, stdin: int) -> Any:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "dsbot.files",
        args,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        **user,
    )


async def _probe(user: dict[str, Any]) -> str:
    """不给任何文件、只开一个子进程看能不能启动。返回 ok / permission / other。"""
    try:
        proc = await _spawn(_child_args(probe=True), user, stdin=asyncio.subprocess.DEVNULL)
    except OSError as exc:
        return "permission" if exc.errno in (errno.EACCES, errno.EPERM) else "other"
    except (NotImplementedError, ValueError):
        return "other"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return "other"
    if out.startswith(CHILD_READY.encode()):
        return "ok"
    return "permission" if _looks_like_permission_problem(err) else "other"


async def _check_dropping(failure: str, detail: str) -> None:
    """以 nobody 运行的子进程没启动起来。如果确实是 nobody 没权限（不带任何文件再试一次确认，当前用户能启动），
    以后就不降权了；内存不够之类的暂时问题不算，下次照样降权。触发这次失败的文件不会改用当前用户去读。"""
    global _checking_drop, _drop_privileges
    if failure != "permission" or _checking_drop or not _drop_privileges:
        return
    _checking_drop = True
    try:
        if await _probe(dict(_NOBODY)) == "permission" and await _probe({}) == "ok":
            _drop_privileges = False
            log.warning("解析文件的子进程以 nobody 运行时没权限启动（%s），以后改用当前用户运行。", detail[-300:].strip())
    finally:
        _checking_drop = False


async def _run_child(raw: bytes, filename: str, content_type: str | None, max_chars: int, timeout: float) -> dict[str, Any]:
    user = _child_user()
    args = _child_args(filename=filename, content_type=content_type, max_chars=max_chars)
    unavailable = {"error": "bot 这边解析文件的进程启动不了，暂时读不了文件", "transient": True}
    try:
        proc = await _spawn(args, user, stdin=asyncio.subprocess.PIPE)
    except (OSError, NotImplementedError, ValueError) as exc:
        log.warning("开不了解析文件的子进程：%r", exc)
        if user:
            permission = isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM)
            await _check_dropping("permission" if permission else "other", repr(exc))
        if classify(filename, content_type)[0] in ("text", "sniff"):
            # 纯文本的处理都是线性的，在主进程里做也安全
            return await asyncio.to_thread(extract_bytes, raw, filename, content_type, max_chars)
        return unavailable
    try:
        out, err = await asyncio.wait_for(proc.communicate(raw), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        log.info("解析文件 %s 超时（%g 秒）", filename, timeout)
        return {"error": "文件太复杂，读了太久没读完"}
    ready = CHILD_READY.encode()
    if not out.startswith(ready):
        # 还没开始读文件就退出了：是 bot 这边的问题（缺库、没权限、内存不够），不是文件的问题
        detail = err.decode("utf-8", "replace")[-500:]
        log.error("解析文件的子进程启动不了：%s", detail)
        if user:
            await _check_dropping("permission" if _looks_like_permission_problem(err) else "other", detail)
        return unavailable
    out = out[len(ready) :]
    if proc.returncode != 0 or not out:
        log.info("解析文件 %s 的子进程失败（返回码 %s）：%s", filename, proc.returncode, err.decode("utf-8", "replace")[-500:])
        return {"error": "文件太复杂或者损坏了，没读出来"}
    try:
        return json.loads(out.decode("utf-8"))
    except ValueError:
        return {"error": "解析失败"}


async def read_file(raw: bytes, filename: str, content_type: str | None, max_chars: int, timeout: float = 30.0) -> FileText:
    """读一个已经下载好的文件（在子进程里解析）。"""
    kind, reason = classify(filename, content_type)
    if kind == "unsupported":
        return FileText(error=reason)
    return FileText.from_dict(await _run_child(raw, filename, content_type, max_chars, timeout))


class _CappedStderr(io.TextIOBase):
    """pypdf 遇到坏文件会刷大量警告；子进程只留开头一点，免得父进程攒一大堆。"""

    def __init__(self, inner: Any, limit: int = 20_000):
        self.inner = inner
        self.left = limit

    def write(self, text: str) -> int:
        if self.left > 0:
            chunk = text[: self.left]
            self.left -= len(chunk)
            self.inner.write(chunk)
        return len(text)

    def flush(self) -> None:
        self.inner.flush()


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:  # Windows
        return
    for limit, value in ((resource.RLIMIT_AS, CHILD_MEMORY_BYTES), (resource.RLIMIT_CPU, CHILD_CPU_SECONDS)):
        try:
            soft, hard = resource.getrlimit(limit)
            value = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (value, hard))
        except (ValueError, OSError):
            pass


def _child_main() -> None:
    import importlib
    import warnings

    _limit_resources()
    sys.stderr = _CappedStderr(sys.stderr)
    warnings.simplefilter("ignore")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    args = json.loads(sys.argv[1])  # bot 自己生成的参数
    for name in args.get("libs") or []:
        importlib.import_module(name)  # 装了的库先导入：导入不了（比如没权限读）就在这里失败，不会打印下面这行
    sys.stdout.write(CHILD_READY)  # 启动好了；文件内容到这之后才读
    sys.stdout.flush()
    if args.get("probe"):
        return
    try:
        raw = sys.stdin.buffer.read()
        result = extract_bytes(raw, str(args["filename"]), args.get("content_type"), int(args["max_chars"]))
    except MemoryError:
        result = {"error": "文件太复杂，读的时候内存不够"}
    except Exception as exc:
        result = {"error": f"解析失败（{type(exc).__name__}）"}
    sys.stdout.write(json.dumps(result))  # 纯 ASCII 输出，不会因为奇怪的字符写不出去
    sys.stdout.flush()


if __name__ == "__main__":
    _child_main()
