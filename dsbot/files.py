"""Read files that people upload: text / code, PDF, Word, Excel, PowerPoint.

DeepSeek's API only accepts images, so documents have to be turned into text on our side first.

Every file is parsed in a subprocess with memory and CPU limits and is killed on timeout, so a broken or malicious file can at most make that one read fail.
When the bot runs as root (the default in Docker), the subprocess drops to the nobody user and can't read the bot process's environment variables (token, API key).

Run on its own (subprocess entry point): python -m dsbot.files '{"filename": ..., "content_type": ..., "max_chars": ...}' < file
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
CHILD_MEMORY_BYTES = 1024 * 1024 * 1024  # the subprocess may use at most 1 GB of memory
CHILD_CPU_SECONDS = 60
CHILD_UID = 65534  # nobody
MAX_XML_BYTES = 25 * 1024 * 1024  # max uncompressed size of a single XML part in Word / PowerPoint (guards against zip bombs)
MAX_SHEET_BYTES = 400 * 1024 * 1024  # Excel sheets are parsed while decompressing and reading stops once there's enough, so this cap can be larger
MAX_SHARED_STRINGS = 300_000
MAX_PDF_PAGES = 300
MAX_SHEETS = 10
MAX_COLUMNS = 50
SCAN_PAGES = 3  # at most this many page images for a scanned PDF
SNIFF_MAX_BYTES = 2 * 1024 * 1024  # unknown extension: only download it to check whether it's text if it's no bigger than this
DETECT_SAMPLE = 200_000
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # password-protected Office files, old .doc/.xls/.ppt formats

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
    "old Word format (.doc); save it as .docx or PDF and send it again": ".doc .dot .wps",
    "old Excel format (.xls); save it as .xlsx or CSV and send it again": ".xls .xlt .et",
    "old PowerPoint format (.ppt); save it as .pptx or PDF and send it again": ".ppt .pot .dps",
    "RTF can't be read reliably; save it as .docx or PDF and send it again": ".rtf",
    "archive file; archives can't be unpacked": ".zip .rar .7z .tar .gz .tgz .bz2 .xz .zst .lz .cab",
    "audio file; audio can't be listened to": ".mp3 .wav .flac .m4a .aac .ogg .oga .opus .wma .amr .mid .midi",
    "video file; video can't be watched": ".mp4 .mov .mkv .avi .webm .flv .wmv .m4v .3gp .m2ts",
    "program or installer": ".exe .msi .apk .ipa .dmg .pkg .deb .rpm .iso .img .bin .dll .so .dylib .jar .class .pyc .o",
    "this image format can't be viewed; send a PNG or JPG instead": ".heic .heif .avif .bmp .tif .tiff .psd .ai .raw .cr2 .nef .arw .dng .eps .xcf",
    "this format can't be read": ".pages .numbers .key .odt .ods .odp .epub .mobi .azw3 .xps .djvu .sqlite .db .ttf .otf .woff .woff2",
}
UNSUPPORTED_EXTS = {ext: reason for reason, exts in _UNSUPPORTED_GROUPS.items() for ext in exts.split()}


class TooLarge(Exception):
    pass


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def classify(filename: str, content_type: str | None) -> tuple[str, str]:
    """Returns (kind, why it can't be read). kind: text / pdf / docx / xlsx / pptx / sniff (unknown, download it and take a look) / unsupported"""
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
        return "unsupported", "audio file; audio can't be listened to"
    if ctype.startswith("video/"):
        return "unsupported", "video file; video can't be watched"
    if ctype.startswith("image/"):
        return "unsupported", "this image format can't be viewed; send a PNG or JPG instead"
    return "sniff", ""


def max_bytes_for(kind: str, limit: int) -> int:
    return min(limit, SNIFF_MAX_BYTES) if kind == "sniff" else limit


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

_UTF16_32 = ["utf_16", "utf_16_be", "utf_16_le", "utf_32", "utf_32_be", "utf_32_le"]
_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_KANA = re.compile(r"[\u3040-\u30ff]")  # full-width hiragana and katakana
_HALF_KANA = re.compile(r"[\uff61-\uff9f]")  # half-width katakana
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# Common Korean syllables (over 90% of the characters in everyday text are in here). When Chinese is mistaken for a Korean encoding, most of the decoded syllables aren't
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
        return ""  # 7-bit escape-based encodings can't be confused with GBK / Big5
    if e in ("cp949", "euc_kr", "johab", "uhc"):
        return "ko"
    if e.startswith("euc_j"):
        return "ja-euc"
    if "jis" in e or e == "cp932":
        return "ja-sjis"
    return ""


def _plausible(text: str, language: str, *, min_hangul: int = 10) -> bool:
    """When the detector says a Korean / Japanese encoding, check whether the decoded text looks like real Korean / Japanese.

    Chinese text (short text especially) is often misdetected as these encodings, and misdecoded text has telltale signs:
    - read as Korean: most of the decoded Hangul syllables are uncommon ones, or Han characters make up 30% or more;
    - read as Shift_JIS: GBK / Big5 lead bytes mostly fall in the half-width katakana range, giving lots of half-width katakana;
    - read as EUC-JP: GB2312 characters turn into other Han characters, but with almost no kana.
    Too little Hangul (fewer than 10) doesn't count either: the bot was built for Chinese-speaking servers, so when unsure it assumes Chinese.
    When language is zh-gb / zh-big5, check whether it looks like Chinese (only used when the detector gave no answer).
    """
    han = len(_HAN.findall(text))
    if language == "ko":
        hangul = _HANGUL.findall(text)
        if not hangul:
            return False
        frequent = sum(1 for c in hangul if c in _FREQUENT_HANGUL)
        if len(hangul) < min_hangul:
            # stricter when there are few characters: no Han characters, and every syllable is a common one
            return len(hangul) >= 4 and han == 0 and frequent == len(hangul)
        return frequent >= len(hangul) * 0.75 and han <= len(hangul) * 0.3
    kana = len(_KANA.findall(text))
    if language == "ja-euc":
        return kana > 0
    if language == "ja-sjis":
        half = len(_HALF_KANA.findall(text))
        return kana + han > 0 and half <= (kana + han + half) * 0.3
    # Chinese (only used to pick an encoding when the detector gives up): nearly all characters are common ones, with almost no kana, Hangul or private-use characters
    chars = _HAN.findall(text)
    if not chars:
        return False
    junk = kana + len(_HANGUL.findall(text)) + len(_HALF_KANA.findall(text)) + len(_PUA.findall(text))
    common = sum(1 for c in chars if _is_common_hanzi(c, language))
    return junk <= len(chars) * 0.05 and common >= len(chars) * 0.6


def _is_common_hanzi(char: str, language: str) -> bool:
    """GB2312 level-1 characters (the 3755 common simplified characters) / Big5 common characters (5401)."""
    try:
        if language == "zh-gb":
            return 0xB0 <= char.encode("gb2312")[0] <= 0xD7
        return 0xA440 <= int.from_bytes(char.encode("big5"), "big") <= 0xC67E
    except UnicodeEncodeError:
        return False


def _sample(raw: bytes) -> bytes:
    """The leading chunk used for encoding detection. Cut at a line break so a double-byte character isn't split in half, or detection fails."""
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


# Kana that are rare in Japanese: small あいうえお, ゎゐゑゔゕゖ, standalone (semi-)voiced marks, etc. They often show up when common Big5 characters are decoded as EUC-JP
_RARE_KANA = re.compile(r"[\u3041\u3043\u3045\u3047\u3049\u308e\u3090\u3091\u3094-\u3096\u3099-\u309c\u30ee\u30f0\u30f1\u30f7-\u30fa]")


def _clearly_japanese(text: str) -> bool:
    kana = len(_KANA.findall(text))
    return kana >= 3 and kana >= _cjk_count(text) * 0.3 and not _RARE_KANA.search(text)


def _lenient(sample: bytes, encoding: str) -> str | None:
    """Decode leniently: a few bad bytes (e.g. custom characters in Big5) are fine, but lots of them mean it isn't this encoding."""
    text = sample.decode(encoding, "replace")
    bad = text.count("\ufffd")
    non_ascii = len(text) - len(text.encode("ascii", "ignore"))
    return None if bad > max(2, non_ascii * 0.01) else text


def _choose_encoding(sample: bytes, from_bytes: Any) -> str | None:
    """Pick an encoding by charset_normalizer's ranking, but don't take "Korean / Japanese" guesses at face value (short Chinese text is often misdetected as those)."""
    excluded = list(_UTF16_32)
    trusted = True  # until a Korean / Japanese guess has been rejected, accept the detector's results as they are
    first_rejected: str | None = None  # the detector's first Korean / Japanese guess that got rejected
    weak_guess: str | None = None  # looks Korean, but too few characters to be sure
    chinese: list[str | None] = []

    def chinese_reading() -> str | None:
        """Whether it reads as Chinese (GB18030 / Big5), as judged by the detector. Asked only once."""
        if not chinese:
            best = from_bytes(sample, cp_isolation=["gb18030", "big5"]).best()
            chinese.append(best.encoding if best is not None else None)
        return chinese[0]

    if from_bytes is not None:
        for _ in range(3):
            results = from_bytes(sample, cp_exclusion=excluded)
            if not results:
                break
            for match in results:  # already sorted by confidence
                encoding = match.encoding
                language = _language_of(encoding)
                if not language:
                    if trusted:
                        return encoding
                    if encoding in _CHINESE and chinese_reading():
                        return chinese_reading()
                    continue  # after rejecting Korean / Japanese, the other encodings ranked below can't be trusted (Big5-HKSCS, Arabic and such when it doesn't read as Chinese)
                text = sample.decode(encoding, "replace")
                if _plausible(text, language):
                    if (
                        language == "ja-euc"
                        and _cjk_count(text) < SHORT_TEXT
                        and not _clearly_japanese(text)
                        and chinese_reading()
                    ):
                        return chinese_reading()  # short EUC-JP and Big5 are hard to tell apart from the characters alone; lean toward Chinese (the bot was built for Chinese-speaking servers)
                    return encoding
                if weak_guess is None and language == "ko" and _plausible(text, language, min_hangul=1):
                    weak_guess = encoding
                first_rejected = first_rejected or encoding
                trusted = False
                excluded += [e for e in _FAMILIES[language] if e not in excluded]
            if chinese_reading():
                return chinese_reading()  # claimed Korean / Japanese but doesn't look like it, and it reads fine as Chinese: most likely Chinese
    # the detector gave nothing usable (e.g. Japanese game scripts full of symbols, Big5 files with custom characters): try them one by one
    for encoding, language in _FALLBACKS:
        if language.startswith("zh") and first_rejected:
            continue  # the detector already decided it doesn't read as Chinese
        text = _lenient(sample, encoding)
        if text is not None and _plausible(text, language):
            return encoding
    # still nothing: trust the detector's original guess (e.g. Japanese spreadsheets full of half-width katakana, Korean heavy in Han characters)
    return weak_guess or first_rejected


SHORT_TEXT = 40  # fewer CJK characters than this counts as short text
# The order to try when the detector gives up. Korean has the strictest check (Chinese almost never passes it), so it goes first;
# EUC-JP kana happen to be a batch of common characters in Big5, so Big5 goes after Japanese (Big5 decoded as EUC-JP has lots of bad bytes, so it won't be mistaken)
_FALLBACKS = [
    ("cp949", "ko"),
    ("gb18030", "zh-gb"),
    ("cp932", "ja-sjis"),
    ("euc_jp", "ja-euc"),
    ("big5", "zh-big5"),
]


def decode_text(raw: bytes) -> str | None:
    """Decode a text file; returns None if it looks binary. UTF-8 first; older files can also be GBK/GB18030, Big5, or legacy Japanese and Korean encodings."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8", "replace")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):  # must be checked before UTF-16, since their BOMs start the same way
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
    """Normalize line breaks, strip trailing whitespace, collapse extra blank lines. All linear-time (stripping trailing whitespace with a regex gets very slow on long runs of spaces)."""
    text = clean_text(text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cut(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut to max_chars, at a line break when possible."""
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    newline = head.rfind("\n")
    if newline > max_chars * 0.9:
        head = head[:newline]
    return head.rstrip(), True


def _notebook(text: str) -> str:
    """Jupyter notebook: keep only the cells' code and text, not the outputs (outputs often hold huge base64 images)."""
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
        return {"error": "not a text file; this format can't be read"}
    if filename.lower().endswith(".ipynb"):
        text = _notebook(text)
    text = text.lstrip()  # a big run of leading whitespace isn't content and shouldn't eat into the window below
    window = max_chars * 2 + 10_000
    work = tidy(text[:window])  # only tidy the part that will be used
    if not work:
        return {"kind": "text file", "detail": "the file is empty"}
    body, truncated = cut(work, max_chars)
    truncated = truncated or len(text) > window
    detail = f"read only the first {len(body)} of about {len(text)} characters" if truncated else ""
    return {"kind": "text file", "text": body, "detail": detail, "truncated": truncated}


def _count(n: int, word: str) -> str:
    """'1 page', '3 pages'."""
    return f"{n} {word}" + ("" if n == 1 else "s")


# ---------------------------------------------------------------------------
# PDF (pypdf)
# ---------------------------------------------------------------------------


def _pdf(raw: bytes, max_chars: int) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"error": "pypdf isn't installed properly on the bot's side, so PDFs can't be read for now", "transient": True}

    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        # many PDFs only restrict printing/copying and need no password to open, so an empty password unlocks them
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:
            if type(exc).__name__ == "DependencyError":
                return {"error": "encrypted PDF, and the bot is missing the decryption component (pypdf[crypto])"}
            unlocked = 0
        if not unlocked:
            return {"error": "the PDF needs a password to open, so it can't be read"}

    total = len(reader.pages)
    if not total:
        return {"error": "the PDF is empty"}
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
            block = f"[Page {index + 1}]\n{page_text}"
            parts.append(block)
            used += len(block) + 2
            real_chars += len(re.sub(r"\s", "", page_text))

    images: list[str] = []
    if real_chars < 20 * max(1, min(done, 3)):
        # very little text: probably a scan or a PDF made from photos, so attach images of the first few pages
        images = _pdf_page_images(reader, min(total, SCAN_PAGES))
    body, truncated = cut("\n\n".join(parts), max_chars)
    if not body and not images:
        return {"error": f"the PDF ({_count(total, 'page')}) has no readable text"}
    detail = _count(total, "page")
    if body and truncated:
        pages = re.findall(r"^\[Page (\d+)\]$", body, re.M)
        detail += f", text only read up to page {pages[-1] if pages else 1}"
    elif body and done < total:
        detail += f", text only read up to page {done}"
    if images:
        shown = "an image of the first page is" if len(images) == 1 else f"images of the first {len(images)} pages are"
        detail += f", very little text (probably a scan), so {shown} attached"
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
# Office (docx / xlsx / pptx are all zip + XML, read directly with the standard library)
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
    """The main document part is usually at a fixed path, but some software puts it elsewhere (e.g. word/document2.xml); _rels/.rels is what counts."""
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
    """One paragraph → [paragraph text, lines from text boxes in the paragraph…]."""
    out: list[str] = []
    extra: list[str] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            tag = child.tag
            if tag == MC + "Fallback":
                continue  # text boxes and the like are stored in both Choice and Fallback; read only one copy
            if tag == W + "t":
                out.append(child.text or "")
            elif tag == W + "tab":
                out.append("\t")
            elif tag in (W + "br", W + "cr"):
                out.append("\n")
            elif tag == W + "txbxContent":
                _docx_block(child, extra)  # paragraphs in a text box go on their own lines
            else:
                walk(child)

    walk(p)
    text = "".join(out)
    ppr = p.find(W + "pPr")
    if text.strip() and ppr is not None:
        style_node = ppr.find(W + "pStyle")
        style = ((style_node.get(W + "val") if style_node is not None else "") or "").lower()
        if style.startswith(("heading", "title")):
            text = "## " + text  # heading
        elif ppr.find(W + "numPr") is not None or "list" in style:
            text = "• " + text  # list item
    return [text] + extra


def _wrapped(child: ET.Element) -> ET.Element | None:
    """Content controls (sdt), customXml, compatibility wrappers: return the real content inside."""
    if child.tag == W + "sdt":
        return child.find(W + "sdtContent")
    if child.tag == W + "customXml":
        return child
    if child.tag == MC + "AlternateContent":
        return child.find(MC + "Choice")  # Fallback is the same content; read only one copy
    return None


def _children(container: ET.Element, tag: str) -> Iterator[ET.Element]:
    """Elements of one kind inside a container (table rows, cells in a row), looking through the various wrappers."""
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
                    _docx_block(cell, cell_lines)  # direct children only, so text boxes aren't read several times
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
        return {"error": "not a valid Word file"}
    lines: list[str] = []
    _docx_block(body, lines)
    text = tidy("\n".join(lines))
    if not text:
        return {"kind": "Word document", "detail": "there's no text in it (maybe only images)"}
    out, truncated = cut(text, max_chars)
    detail = f"read only the first {len(out)} of about {len(text)} characters" if truncated else ""
    return {"kind": "Word document", "text": out, "detail": detail, "truncated": truncated}


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
            out.append("\n")  # soft line break inside a paragraph
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
                block = f"[Slide {number}]\n" + "\n".join(paras)
                parts.append(block)
                used += len(block) + 2
    if not slides:
        return {"error": "not a valid PowerPoint file"}
    if not parts:
        return {"kind": "PowerPoint", "detail": f"{_count(len(slides), 'slide')}, there's no text in it (maybe only images)"}
    body, truncated = cut(tidy("\n\n".join(parts)), max_chars)
    detail = _count(len(slides), "slide")
    if truncated:
        pages = re.findall(r"^\[Slide (\d+)\]$", body, re.M)
        detail += f", only read up to slide {pages[-1] if pages else 1}"
    elif done < len(slides):
        detail += f", only read up to slide {done}"
    return {"kind": "PowerPoint", "text": body, "detail": detail, "truncated": truncated or done < len(slides)}


_BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))
_BUILTIN_PERCENT_FORMATS = {9, 10}


def _format_kind(code: str) -> str | None:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)  # drop quoted text, [colors] and escaped characters
    if "%" in code:
        return "percent"
    if re.search(r"[yYdD]|[hH].*[mM]|[mM].*[sS]", code):
        return "date"
    return None


def _cell_styles(z: zipfile.ZipFile) -> dict[int, str]:
    """Cell style index → "date" / "percent"."""
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
    return "".join(parts)  # skip rPh (phonetic guides)


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
    """Turn one table row into text. Sparse rows with lots of gaps become "Z6=value" instead, to avoid long runs of empty columns."""
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
                    break  # the strings the first rows use come first, so this is enough
    return out


def _sheet_rows(z: zipfile.ZipFile, path: str) -> Iterator[ET.Element]:
    """Parse a worksheet while decompressing it, yielding it row by row; the caller stops once it has enough, so big sheets never have to fit in memory."""
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
            return {"error": "not a valid Excel file"}
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
            header = f"[Sheet: {name}]"
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
                        rows.append(line[:budget])  # even the first row is too long: cut it, which beats saying "the sheet is empty"
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
        return {"kind": "Excel spreadsheet", "detail": f"{_count(len(sheets), 'sheet')}, all empty"}
    truncated = truncated or len(sheets) > MAX_SHEETS
    detail = _count(len(sheets), "sheet") + (", too long, so only the first part was read" if truncated else "")
    return {"kind": "Excel spreadsheet", "text": tidy("\n\n".join(parts)), "detail": detail, "truncated": truncated}


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def _clean_result(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("kind", "text", "detail", "error"):
        if isinstance(result.get(key), str):
            result[key] = clean_text(result[key])
    return result


def extract_bytes(raw: bytes, filename: str, content_type: str | None, max_chars: int) -> dict[str, Any]:
    """Pure function: turn file content into text. Returns a dict that can be turned into a FileText."""
    kind, reason = classify(filename, content_type)
    if kind == "unsupported":
        return {"error": reason}
    if kind in ("docx", "xlsx", "pptx") and raw.startswith(OLE_MAGIC):
        return {"error": "this Office file is password-protected (or is actually in an old format) and can't be read"}
    readers = {"text": _text, "sniff": _text, "pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "pptx": _pptx}
    try:
        if kind in ("text", "sniff"):
            return _clean_result(_text(raw, filename, max_chars))
        return _clean_result(readers[kind](raw, max_chars))
    except MemoryError:
        return {"error": "the file is too complex; ran out of memory while reading it"}
    except TooLarge:
        return {"error": "the content inside the file is too large to read"}
    except (zipfile.BadZipFile, zlib.error, EOFError):
        return {"error": "the file is corrupted and can't be opened"}
    except Exception as exc:
        name = type(exc).__name__
        if name in ("PdfReadError", "PdfStreamError", "EmptyFileError"):
            return {"error": "the PDF is corrupted and can't be opened"}
        if isinstance(exc, (ET.ParseError, ValueError, KeyError)):
            return {"error": "the file is corrupted or in the wrong format and can't be opened"}
        return {"error": f"parsing failed ({name})"}


def _import_paths() -> list[str]:
    return [p for p in sys.path if p and os.path.exists(p)]


def _child_env() -> dict[str, str]:
    """The subprocess gets only a minimal set of environment variables, so while parsing untrusted files it can't reach the token or API key.

    PYTHONPATH is set to the bot's own search path, so libraries installed under root's home are still found after dropping to nobody.
    """
    keep = ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
    env["PYTHONPATH"] = os.pathsep.join(_import_paths())
    return env


def _others_can_use(path: str | Path, *, read: bool) -> bool:
    """Whether other users (nobody) can use this path: every directory on the way can be entered, and the target itself can be read/executed."""
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
_CHILD_LIBRARIES = ("pypdf", "charset_normalizer", "cryptography", "PIL")  # third-party libraries the subprocess uses


def _child_needs() -> list[tuple[str | Path, bool]]:
    """What the subprocess needs: (path, whether it needs read or execute access)."""
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
    """When the bot runs as root (the default in Docker), the subprocess drops to nobody so it can't read the bot process's /proc/…/environ (token, API key).

    It only drops privileges if nobody can read Python, the bot's code and the installed libraries; otherwise (e.g. libraries installed under /root)
    it runs as usual and warns once. If the permission bits look fine but nobody still can't start (e.g. ACLs), _check_dropping confirms that and stops dropping.
    """
    global _drop_privileges
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return {}
    if _drop_privileges is None:
        _drop_privileges = all(_others_can_use(p, read=r) for p, r in _child_needs())
        if not _drop_privileges:
            log.warning(
                "The file-parsing subprocess can't drop to the nobody user (other users can't read the directories holding Python, "
                "the bot's code or the installed libraries), so it will run as the current user. Deploying with Docker avoids this."
            )
    return dict(_NOBODY) if _drop_privileges else {}


CHILD_READY = "dsbot-child-ready\n"  # the subprocess prints this line once its libraries are imported, before it starts reading the file
_PRELOAD = ("pypdf", "charset_normalizer", "PIL.Image")  # imported when the subprocess starts (if installed); failing to import counts as a startup failure
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
    """The subprocess exited before reading the file: check whether it lacked permission to read the code or libraries (it hadn't touched the file yet, so its output can be trusted)."""
    text = stderr.decode("utf-8", "replace")
    return any(s in text for s in ("PermissionError", "Permission denied", "ModuleNotFoundError", "ImportError"))


def _child_args(**extra: Any) -> str:
    return json.dumps({**extra, "libs": _preload_libraries()})  # pure ASCII


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
    """Start a subprocess without any file, just to see whether it can start. Returns ok / permission / other."""
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
    """A subprocess running as nobody failed to start. If nobody really lacks permission (confirmed by retrying without any file while
    the current user can start one), stop dropping privileges from now on; temporary problems such as low memory don't count, and the
    next one drops privileges again. The file that triggered the failure is never read as the current user instead."""
    global _checking_drop, _drop_privileges
    if failure != "permission" or _checking_drop or not _drop_privileges:
        return
    _checking_drop = True
    try:
        if await _probe(dict(_NOBODY)) == "permission" and await _probe({}) == "ok":
            _drop_privileges = False
            log.warning("The file-parsing subprocess has no permission to start as nobody (%s); running it as the current user from now on.", detail[-300:].strip())
    finally:
        _checking_drop = False


async def _run_child(raw: bytes, filename: str, content_type: str | None, max_chars: int, timeout: float) -> dict[str, Any]:
    user = _child_user()
    args = _child_args(filename=filename, content_type=content_type, max_chars=max_chars)
    unavailable = {"error": "the bot's file-parsing process couldn't start, so files can't be read for now", "transient": True}
    try:
        proc = await _spawn(args, user, stdin=asyncio.subprocess.PIPE)
    except (OSError, NotImplementedError, ValueError) as exc:
        log.warning("Couldn't start the file-parsing subprocess: %r", exc)
        if user:
            permission = isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM)
            await _check_dropping("permission" if permission else "other", repr(exc))
        if classify(filename, content_type)[0] in ("text", "sniff"):
            # plain-text handling is all linear-time, so it's safe to do in the main process
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
        log.info("Parsing file %s timed out (%g s)", filename, timeout)
        return {"error": "the file is too complex; reading it took too long"}
    ready = CHILD_READY.encode()
    if not out.startswith(ready):
        # it exited before reading the file: a problem on the bot's side (missing library, no permission, out of memory), not with the file
        detail = err.decode("utf-8", "replace")[-500:]
        log.error("The file-parsing subprocess couldn't start: %s", detail)
        if user:
            await _check_dropping("permission" if _looks_like_permission_problem(err) else "other", detail)
        return unavailable
    out = out[len(ready) :]
    if proc.returncode != 0 or not out:
        log.info("The subprocess parsing %s failed (exit code %s): %s", filename, proc.returncode, err.decode("utf-8", "replace")[-500:])
        return {"error": "the file is too complex or corrupted; nothing could be read"}
    try:
        return json.loads(out.decode("utf-8"))
    except ValueError:
        return {"error": "parsing failed"}


async def read_file(raw: bytes, filename: str, content_type: str | None, max_chars: int, timeout: float = 30.0) -> FileText:
    """Read a file that's already been downloaded (parsed in a subprocess)."""
    kind, reason = classify(filename, content_type)
    if kind == "unsupported":
        return FileText(error=reason)
    return FileText.from_dict(await _run_child(raw, filename, content_type, max_chars, timeout))


class _CappedStderr(io.TextIOBase):
    """pypdf spews warnings on broken files; the subprocess keeps only the beginning so the parent doesn't pile up a huge amount."""

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
    args = json.loads(sys.argv[1])  # arguments generated by the bot itself
    for name in args.get("libs") or []:
        importlib.import_module(name)  # import the installed libraries first: if that fails (e.g. no permission to read them), it fails here and the line below is never printed
    sys.stdout.write(CHILD_READY)  # started up; the file content is only read after this
    sys.stdout.flush()
    if args.get("probe"):
        return
    try:
        raw = sys.stdin.buffer.read()
        result = extract_bytes(raw, str(args["filename"]), args.get("content_type"), int(args["max_chars"]))
    except MemoryError:
        result = {"error": "the file is too complex; ran out of memory while reading it"}
    except Exception as exc:
        result = {"error": f"parsing failed ({type(exc).__name__})"}
    sys.stdout.write(json.dumps(result))  # pure ASCII output, so odd characters can't make the write fail
    sys.stdout.flush()


if __name__ == "__main__":
    _child_main()
