"""Conservative, deterministic lane selection; no file contents or AI needed.

Prefix matching intentionally accepts false positives (e.g. author -> auth,
product -> prod, postgres -> post, tagline -> tag, apply formatting -> apply).
Fast additionally requires a complete, bounded editing production below;
recognizing individual words is never sufficient. Project maps add risk only.
Unknown mentions block fast even alongside a known target.
Only README has a bare-name convenience rule; other symbols need backticks.
Outward-action and destructive stems count only as plain prose words: a stem
that is merely part of an identifier (snake/camel case, dotted names and calls,
file-like tokens, backticked names) is not that action. Kebab-case and
slash-joined words (force-push, commit/push) remain prose. Risk stems are never
loosened and additionally match camelCase parts (userAuth -> auth).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from orcexe import globs, gitutil, miniyaml


@dataclass(frozen=True)
class Signal:
    id: str
    detail: str


@dataclass(frozen=True)
class Classification:
    mode: str
    lane: str
    targets: tuple
    signals: tuple


@dataclass(frozen=True)
class RiskMap:
    """Immutable additions; defaults are always applied independently."""

    risky_words: tuple = ()
    risky_paths: tuple = ()

    def __post_init__(self):
        for key in ("risky_words", "risky_paths"):
            values = getattr(self, key)
            if not isinstance(values, (list, tuple)):
                raise TypeError(key + " must be a sequence of strings")
            if any(not isinstance(value, str) for value in values):
                raise TypeError(key + " must contain strings")
            if any(not value.strip() or "\0" in value for value in values):
                raise ValueError(key + " must contain nonempty, NUL-free strings")
            if key == "risky_words" and any(not _words(value) for value in values):
                raise ValueError(key + " must contain letters or digits")
            object.__setattr__(self, key, tuple(values))


_RISK_WORDS = tuple("""auth login oauth sso password passwd secret token credential
apikey api_key private_key payment billing invoice stripe checkout migration migrate
schema deploy production prod release security permission encrypt crypto şifre parola
ödeme fatura migrasyon göç yetki güvenlik canlı sır anahtar
env environment pii gdpr kvkk personal kişisel customer müşteri ssn tckn kimlik""".split()) + (
    "ortam değişken", "kullanıcı veri", "user data",
)
_DESTRUCTIVE = tuple("""delete remove rm drop truncate purge wipe erase sil kaldır
temizle boşalt""".split()) + ("yok et",)
_OUTWARD = tuple("""push publish deploy commit merge rebase tag upload send email
mail post webhook curl wget ssh scp rsync kubectl terraform helm twine gh apply
gönder yayınla yükle paylaş birleştir drop alter truncate grant revoke database
table""".split()) + ("docker push", "npm publish", "pip upload", "insert into", "delete from")
_LOCKS = """package-lock.json yarn.lock pnpm-lock.yaml poetry.lock uv.lock Pipfile.lock
Cargo.lock Gemfile.lock composer.lock go.sum bun.lockb""".split()
_RISK_PATHS = (
    ".github/**", ".gitlab-ci.yml", ".circleci/**", "**/migrations/**",
    "**/migrate/**", "**/.env*", "**/*secret*", "**/*auth*", "**/*payment*",
    "**/*billing*", "**/*.pem", "**/*.key", "**/Dockerfile*", "**/*.tf",
) + tuple("**/" + name for name in _LOCKS)
_BROAD = """all every entire whole across everywhere refactor rewrite migrate tüm
bütün hepsi her baştan refaktör global globally everything""".split() + [
    "yeniden yaz", "her yer", "her şey", "project-wide", "proje genel",
]
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MENTION = re.compile(r"[^\s`'\"’‘“”()\[\]{}<>,;:!?]+")
_BACKTICK = re.compile(r"`([^`]+)`")

_DOC_EXTENSIONS = frozenset(".md .markdown .rst .txt .adoc".split())
_SOURCE_EXTENSIONS = frozenset(""".py .js .mjs .cjs .ts .tsx .jsx .go .rs .java
.kt .swift .rb .php .c .h .cc .cpp .hpp .cs""".split())
_DENIED_DIRECTORIES = frozenset("""scripts bin tools hooks .husky .git .github .gitlab .circleci ci deploy
infra ops vendor third_party third-party node_modules .venv venv env site-packages
dist build out target .next coverage __generated__ generated gen
i18n l10n locale locales translations""".split())
_DENIED_SOURCE_NAMES = frozenset("setup.py conftest.py manage.py noxfile.py fabfile.py".split())
# Turkish inflection is bounded: zero to three members of this exact set.
# nı/ni/nu/nü cover possessive accusatives, e.g. hata + sı + nı.
_FAST_SUFFIX_TEXT = """ı i u ü a e ya ye yı yi da de ta te dan den tan ten
nın nin nun nün ın in un ün ler lar leri ları sı si su sü na ne ndan nden nda
nde ki deki daki ını ini nı ni nu nü"""
_FAST_NOUN_TEXT = """docstring comment typo section example paragraph installation
usage kurulum kullanım bölüm örnek paragraf yazım hata yorum
name date list table text heading title spelling punctuation word sentence
ad tarih liste tablo metin başlık imla noktalama kelime cümle"""
# A small subset of these nouns appears in the productions below. Recognizing a
# word never grants fast by itself. örneğ is the softened stem in örneği.
_FAST_LEXEME = re.compile(r"[^\W_]+|[^\s]", re.UNICODE)
_TARGET_SUFFIX = re.compile(r"['’]([^\W\d_]+)", re.UNICODE)
_FAST_ACTIONS = frozenset("add fix update clarify ekle düzelt güncelle netleştir".split())
# Full-task productions: (intent, target class, pattern). @target is inserted
# only for a resolved mention; user punctuation cannot create this sentinel.
_FAST_PATTERNS = (
    ("add_docstring", "source", r"(?:add (?:a )?docstring to @target|@target docstring ekle)"),
    ("add_comment", "source", r"(?:add (?:a )?comment to @target|@target yorum ekle)"),
    ("add_section", "doc", r"(?:add (?:(?:a )?(?:usage|installation) (?:section|example)|an (?:usage|installation) example) to @target|@target (?:kurulum|kullanim) (?:bölüm|örnek) ekle)"),
    ("add_example", "doc", r"(?:add an example to @target|@target örnek ekle)"),
    ("fix_docstring", "source", r"(?:fix (?:the )?docstring (?:in|of) @target|@target docstring düzelt)"),
    ("fix_typo", "doc source", r"(?:fix (?:the )?typo in @target|@target (?:yorum )?yazim hata düzelt)"),
    ("clarify_paragraph", "doc", r"(?:clarify @target|@target (?:paragraf )?netleştir)"),
    ("update_doc", "doc", r"(?:update @target|@target güncelle)"),
    ("fix_doc", "doc", r"(?:fix @target|@target düzelt|düzelt @target)"),
)


def _fold(text):
    # Normalize compatibility forms before removing all format characters so
    # fullwidth text and invisible separators cannot split a risky stem.
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    # Fold both English and Turkish I variants together. Removing U+0307 also
    # handles decomposed dotted capitals. This errs toward extra risk matches.
    return text.casefold().replace("\u0307", "").replace("ı", "i")


# Used by the v2 base (_v2_field_signals); any change must keep V2BaseTests green.
def _words(text):
    """Unicode letters/digits, split at punctuation (including underscores)."""
    return _WORD.findall(_fold(text))


# Not a word: separates prose runs so a multiword stem cannot span an identifier.
_IDENTIFIER = "\0identifier"
# Markdown emphasis (_word_, *word*) is prose; only inner underscores count.
_EDGE = "\"'`’‘“”()[]{}<>,;:!?.…_*"
# Kebab-case and slash-joined words (force-push, commit/push) stay prose: only
# snake_case, dotted names/file extensions and anchored paths hide a stem.
_IDENTIFIER_SHAPE = re.compile(r"_|[^\W_]\.[^\W_]|^(?:\.{1,2}/|/|~/)", re.UNICODE)
_CALL = re.compile(r"[^\W_]\(", re.UNICODE)


def _visible(text):
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def _camel(chunk):
    """lower->upper inside a chunk (sendEmail, PushNotifier, userAuth).

    Upper-then-lower alone is prose: PUSHsuffix, APIs and PRs stay words.
    """
    return any((a.islower() or a.isdigit()) and b.isupper() for a, b in zip(chunk, chunk[1:]))


def _prose_words(task):
    """Words from plain prose only; each identifier becomes one separator token.

    Case is read before folding (camelCase needs it). A backticked name with
    no whitespace is an identifier as a whole; backticked text with spaces is a
    command line (`git push origin`), whose plain words still count. An
    unbalanced backtick leaves the rest as prose.
    """
    tokens = []
    for index, part in enumerate(re.split(r"`([^`]*)`", _visible(task))):
        if index % 2:
            # Either way the quoted span never joins the surrounding prose.
            tokens.append(_IDENTIFIER)
            if not any(c.isspace() for c in part):
                continue
        for chunk in part.split():
            core = chunk.strip(_EDGE)
            if core and (_IDENTIFIER_SHAPE.search(core) or _CALL.search(chunk) or _camel(core)):
                tokens.append(_IDENTIFIER)
            else:
                tokens.extend(_words(chunk))
    return tokens


def _camel_words(task):
    """Words with camelCase parts split, used only to add risk matches."""
    text = _visible(task)
    split = []
    for i, c in enumerate(text):
        after = text[i + 1] if i + 1 < len(text) else " "
        if i and c.isupper() and (text[i - 1].islower() or text[i - 1].isdigit()
                                  or text[i - 1].isupper() and after.islower()):
            split.append(" ")
        split.append(c)
    return _words("".join(split))


_FAST_SUFFIXES = frozenset(_fold(s) for s in _FAST_SUFFIX_TEXT.split())
_FAST_NOUNS = {_fold(s): _fold(s) for s in _FAST_NOUN_TEXT.split()}
_FAST_NOUNS["örneğ"] = "örnek"
_FAST_ACTIONS = frozenset(_fold(s) for s in _FAST_ACTIONS)
_FAST_VOCABULARY = frozenset(_FAST_NOUNS) | _FAST_ACTIONS | {"a", "an", "the", "to", "in", "of"}


def _fast_suffix(text):
    positions = {0}
    for _ in range(3):
        positions = {i + len(s) for i in positions for s in _FAST_SUFFIXES
                     if text.startswith(s, i)}
        if len(text) in positions:
            return True
    return not text


def _fast_noun(token):
    if token in _FAST_NOUNS:
        return _FAST_NOUNS[token]
    for noun, canonical in _FAST_NOUNS.items():
        if token.startswith(noun) and _fast_suffix(token[len(noun):]):
            return canonical
    return token


def _target_class(path):
    # Resolved mentions must not hide shell syntax or non-Latin lookalikes.
    if any(c not in " _-./" and not (c.isascii() and c.isalnum())
           and not (c.isalpha() and unicodedata.name(c, "").startswith("LATIN "))
           for c in path):
        return None
    segments = _fold(path).split("/")
    name = segments[-1]
    if any(s in _DENIED_DIRECTORIES for s in segments[:-1]):
        return None
    ancestors = ["/".join(segments[:i]) for i in range(1, len(segments) + 1)]
    if (".lock" in name or name.startswith(("license", "licence", "copying", "notice", "manifest"))
            or any(_contains(_words(path), stem) for stem in _RISK_WORDS)
            or any(_glob(p, ancestor) for p in _RISK_PATHS for ancestor in ancestors)):
        return None
    extension = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if extension in _DOC_EXTENSIONS:
        return None if extension == ".txt" and name.startswith(("requirements", "constraints")) else "doc"
    if extension in _SOURCE_EXTENSIONS:
        if (name in _DENIED_SOURCE_NAMES or name.startswith(("gulpfile.", "gruntfile."))
                or any(name.endswith(".config" + ext) for ext in (".js", ".ts", ".mjs", ".cjs"))):
            return None
        return "source"
    return None


def _fast_task(task, resolved_mentions):
    """Mask only resolved mentions; lex every other character, including quotes.

    Double quote delimiters may surround words but never exempt their contents.
    Other punctuation, symbols, numbers and unknown words remain unknown tokens.
    A target plus its closed apostrophe suffix counts as one of the 25 tokens.
    """
    spans = []
    for match in list(_BACKTICK.finditer(task)) + list(_MENTION.finditer(task)):
        quoted = match.re is _BACKTICK
        mention = match.group(1) if quoted else match.group().rstrip(".")
        normalized = mention
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized not in resolved_mentions:
            continue
        start = match.start()
        end = match.end() if quoted else start + len(mention)
        suffix = _TARGET_SUFFIX.match(task, end)
        if suffix and _fast_suffix(_fold(suffix.group(1))):
            end = suffix.end()
        # Quoting a target must not manufacture word boundaries, e.g.
        # update`README.md` or `README.md`güncelle. Keep existing double quotes.
        left, right = start, end
        while left and task[left - 1] in ('"', '“', '”'):
            left -= 1
        while right < len(task) and task[right] in ('"', '“', '”'):
            right += 1
        if ((left and not task[left - 1].isspace())
                or (right < len(task) and not task[right].isspace())):
            continue
        spans.append((start, end, "." in normalized.rsplit("/", 1)[-1]))
    tokens = []
    explicit = True
    end = 0

    def lex(text):
        return [m.group() for m in _FAST_LEXEME.finditer(_fold(text))
                if m.group() not in ('"', '“', '”')]

    for start, stop, has_extension in sorted(spans, key=lambda s: (s[0], -s[1])):
        if start < end:
            continue
        tokens.extend(lex(task[end:start]))
        tokens.append("@target")
        explicit = explicit and has_extension
        end = stop
    tokens.extend(lex(task[end:]))
    normalized = [_fast_noun(t) for t in tokens]
    unknown = sum(t != "@target" and t not in _FAST_VOCABULARY for t in normalized)
    actions = sum(t in _FAST_ACTIONS for t in normalized)
    text = " ".join(normalized)
    matches = [(intent, classes) for intent, classes, pattern in _FAST_PATTERNS
               if re.fullmatch(pattern, text) and (intent != "fix_doc" or explicit)]
    return tokens, unknown, actions, matches


def _mixed_script(token):
    scripts = {unicodedata.name(c, "").split(" ", 1)[0]
               for c in token if unicodedata.category(c).startswith("L")}
    return "LATIN" in scripts and bool(scripts & {"CYRILLIC", "GREEK"})


def _contains(tokens, stem):
    parts = _words(stem)
    # Compound stems such as api_key survive the non-alphanumeric tokenizer.
    return any(tokens[i:i + len(parts) - 1] == parts[:-1]
               and tokens[i + len(parts) - 1].startswith(parts[-1])
               for i in range(len(tokens) - len(parts) + 1))


def _glob(pattern, path):
    """Segment glob: * and ? stay within a segment; ** spans zero or more.

    No character classes or escapes. A small iterative automaton avoids both
    whole-path fnmatch's slash crossing and recursive/exponential backtracking.
    Patterns are anchored at the repo root; use **/ for any directory depth.
    """
    patterns = globs.path_key(_fold(pattern)).rstrip("/").split("/")
    segments = globs.path_key(_fold(path)).split("/")
    states = {0}

    def closure(current):
        current = set(current)
        for index in range(len(patterns)):
            if index in current and patterns[index] == "**":
                current.add(index + 1)
        return current

    states = closure(states)
    for segment in segments:
        following = set()
        for index in states:
            if index == len(patterns):
                continue
            part = patterns[index]
            if part == "**":
                following.add(index)
            else:
                if _segment_glob(part, segment):
                    following.add(index + 1)
        states = closure(following)
        if not states:
            return False
    return len(patterns) in states


def _segment_glob(pattern, text):
    """Greedy wildcard scan; no regex backtracking on project-owned patterns."""
    p = t = 0
    star = -1
    retry = 0
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, retry = p, t
            p += 1
        elif p < len(pattern) and (pattern[p] == "?" or pattern[p] == text[t]):
            p += 1
            t += 1
        elif star != -1:
            retry += 1
            p, t = star + 1, retry
        else:
            return False
    return all(c == "*" for c in pattern[p:])


def load_risk_map(path) -> RiskMap:
    data = miniyaml.load_path(path)
    if not isinstance(data, dict):
        raise ValueError("risk_map must be a mapping")
    for key in data:
        if key not in ("version", "risky_words", "risky_paths"):
            raise ValueError("unknown key: " + key)
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("version must be 1")
    for key in ("risky_words", "risky_paths"):
        values = data.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(key + " must be a list of strings")
    return RiskMap(tuple(data.get("risky_words", [])), tuple(data.get("risky_paths", [])))


def classify(task: str, files: Sequence[str] | None,
             risk_map: RiskMap | None = None) -> Classification:
    """Evaluate every rule; signals are unique and sorted by (id, detail).

    Ambiguous candidates are included in targets so none can hide path risk.
    Details contain matched stems/patterns or bounded tracked paths, never the
    task text or an unresolved (potentially secret-bearing) token.
    """
    if not isinstance(task, str):
        raise TypeError("task must be a string")
    if files is not None and (not isinstance(files, Sequence) or isinstance(files, (str, bytes))):
        raise TypeError("files must be a sequence of strings or None")
    if risk_map is not None and not isinstance(risk_map, RiskMap):
        raise TypeError("risk_map must be a RiskMap or None")
    risk_map = risk_map or RiskMap()
    mode = "everyday" if files is None else "new" if len(files) == 0 else "existing"
    signals = set()

    def signal(name, detail):
        detail = "".join(c if c.isprintable() else "?" for c in str(detail))[:200]
        signals.add(Signal(name, detail))

    if mode != "existing":
        signal("mode", mode)
    if not task.strip():
        signal("task_empty", "empty")
    if len(task) > 10000:
        signal("task_too_long", len(task))
    if "\0" in task:
        signal("input_nul", "task")
    # Risk matching deliberately folds compatibility/invisible characters;
    # that same folding must never turn disguised text into a fast production.
    nfc_task = unicodedata.normalize("NFC", task)
    if (any(not c.isprintable() or (c.isspace() and c != " ") for c in task)
            or any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN ")
                   for c in task)
            or any(unicodedata.category(c).startswith("M") for c in task)
            or unicodedata.normalize("NFKC", nfc_task) != nfc_task):
        signal("task_unsafe_character", "nonliteral text")
    tokens = _words(task)
    if any(_mixed_script(token) for token in tokens):
        signal("mixed_script", "Latin with Cyrillic/Greek")
    camel_tokens = _camel_words(task)
    for stem in set(_RISK_WORDS + risk_map.risky_words):
        if _contains(tokens, stem) or _contains(camel_tokens, stem):
            signal("risk_word", stem)
    prose = _prose_words(task)
    for stem in _DESTRUCTIVE:
        if _contains(prose, stem):
            signal("destructive", stem)
    for stem in _OUTWARD:
        if _contains(prose, stem):
            signal("outward_action", stem)
    # SQL UPDATE may put a table name and other tokens before SET.
    if any(token.startswith("update") for token in tokens):
        update = next(i for i, token in enumerate(tokens) if token.startswith("update"))
        if any(token.startswith("set") for token in tokens[update + 1:]):
            signal("outward_action", "update … set")
    for stem in _BROAD:
        if _contains(tokens, stem):
            signal("scope_broad", stem)
    if "*" in task:
        signal("scope_broad", "glob")

    mentions = {m.group().rstrip(".") for m in _MENTION.finditer(task)}
    paths = {p for p in mentions if "/" in p or "." in p
             or _fold(p).startswith("dockerfile")}
    quoted = [m.group(1) for m in _BACKTICK.finditer(task)]
    # Quoting is also how filenames containing spaces/newlines can be named.
    paths.update(p for p in quoted if "/" in p or "." in p)
    symbols = {p for p in quoted if p.isidentifier()}
    if any(_fold(p) == "readme" for p in mentions):
        symbols.add("README")
    if any(p not in paths and p not in symbols for p in quoted):
        signal("target_unresolved", "quoted mention")
    if task.count("`") % 2 or "``" in task:
        signal("target_unresolved", "unbalanced or nested backticks")
    normalized = set()
    for path in paths:
        while path.startswith("./"):
            path = path[2:]
        if path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
            signal("path_invalid", "non-relative task path")
        normalized.add(path)
    paths = normalized

    tracked = set()
    basename_matches = {globs.path_key(p.rsplit("/", 1)[-1]): set() for p in paths}
    stem_matches = {globs.path_key(p): set() for p in symbols}
    for path in files if files is not None else ():
        if not isinstance(path, str):
            raise TypeError("files must contain strings")
        if "\0" in path:
            signal("input_nul", "path")
            continue
        if not path or path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/")):
            signal("path_invalid", "non-relative tracked path")
            continue
        tracked.add(path)
        basename = globs.path_key(path.rsplit("/", 1)[-1])
        if basename in basename_matches:
            basename_matches[basename].add(path)
        stem = basename.rsplit(".", 1)[0] if "." in basename else basename
        if stem in stem_matches:
            stem_matches[stem].add(path)

    targets = set()
    resolved_mentions = set()
    ambiguous = False
    for mention in sorted(paths | symbols):
        if mention in paths:
            matches = {p for p in tracked if globs.path_key(p) == globs.path_key(mention)}
            if not matches:
                matches = basename_matches[globs.path_key(mention.rsplit("/", 1)[-1])]
        else:
            matches = stem_matches[globs.path_key(mention)]
        # Folded matches contribute risk but cannot newly grant the fast lane.
        exact = (any(p == mention or ("/" not in mention and p.rsplit("/", 1)[-1] == mention)
                     for p in matches) if mention in paths else
                 any(p.rsplit("/", 1)[-1].rsplit(".", 1)[0] == mention for p in matches))
        if not matches or not exact:
            signal("target_unresolved", "path" if mention in paths else "symbol")
        if len(matches) > 1:
            ambiguous = True
            signal("target_ambiguous", len(matches))
        if len(matches) == 1:
            resolved_mentions.add(mention)
        targets.update(matches)
    for path in targets:
        signal("target_file", path)
    if len(targets) > 1:
        signal("target_ambiguous", len(targets))
    if not targets:
        signal("target_none", "0")
    for path in paths | targets:
        # A file underneath a risky directory inherits its risk. Keep the glob
        # itself segment-anchored; inspect ancestors explicitly instead.
        segments = path.split("/")
        ancestors = ["/".join(segments[:i]) for i in range(1, len(segments) + 1)]
        for pattern in _RISK_PATHS + risk_map.risky_paths:
            if any(_glob(pattern, ancestor) for ancestor in ancestors):
                signal("risk_path", pattern)

    target_class = _target_class(next(iter(targets))) if len(targets) == 1 else None
    signal("target_class" if target_class else "target_class_denied", target_class or "denied")
    fast_tokens, unknown, actions, intents = _fast_task(task, resolved_mentions)
    if len(task) > 200 or len(fast_tokens) > 25:
        signal("task_too_long_for_fast", "length")
    if unknown:
        signal("unrecognized_token", unknown)
    if actions > 1 or len(intents) > 1:
        signal("intent_multiple", max(actions, len(intents)))
    elif len(intents) == 1 and target_class in intents[0][1].split():
        signal("intent", intents[0][0])
    else:
        signal("intent_none", "0")

    risky = any(s.id in ("risk_word", "risk_path", "outward_action") for s in signals)
    blocked = any(s.id not in ("target_file", "target_class", "intent") for s in signals)
    lane = "risky" if risky else "standard" if blocked or ambiguous else "fast"
    return Classification(mode, lane, tuple(sorted(targets)),
                          tuple(sorted(signals, key=lambda s: (s.id, s.detail))))


# --------------------------------------------------------------------------
# lane.v2 (T58): goal + done + notes, the lane only rises.
#
# Normative source: T57 lane design r2 §1.2 and decision 027 Ş1. classify()
# above is lane.v1 and stays unchanged; task_lane() is its union with:
#   K-2  goal identifiers hide an action stem only when they resolve to a
#        tracked file (full path or basename with extension) outside call
#        context; dotted names hide only their tracked .py module part.
#   K-3  action-shape rules A1-A8, in every field.
#   K-4  done/notes: a narrow exact-word risk subset raises; other risk words
#        are informational.
# Fast is produced only by the goal (v1); done/notes can only raise.
#
# lane.v3 (T68; T58 L2 P2-1, P2-2, P2-4 and decision 028):
#   P2-4  a single line break is a space; only a blank line, a bulleted line
#         and .;!? end a sentence. "e.g." "i.e." "vs." "etc." and one letter
#         plus a dot never end one.
#   P2-1  a bare outward verb counts anywhere in a sentence when it has an
#         object (a determiner/pronoun/identifier or to/into/onto right after
#         it) and a direction (to/into/onto within 6 words) or an A8 noun
#         within 3 words; never after a determiner or a negation, never as part
#         of a kebab/slash word or as a compound modifier ("commit message").
#         Passive state sentences ("is pushed", "are committed") count unless
#         negated. "post"/"tag" join A8; a word after ":" starts a sentence;
#         Turkish "<English verb> et/at/yap/edin…", "yayına/canlıya al" and
#         suffix + continuation forms (yükleyip, gönderin) count.
#   P2-2  A5 scans identifier and command parts by prefix; a prose word counts
#         only in its full form and only as the object of the run verb (with at
#         most determiners between). "source" is a run verb only before an
#         identifier or command. A mid-sentence "lead + verb" (A4) needs an
#         object or the sentence end. Passive aorist of ambiguous Turkish stems
#         (yüklenir, birleştirilir, paylaşılır) is a description, not A4-TR.
#   028   an unusable risk map floors classify_repo/task_lane_repo at risky.
#
# T68 r4 architecture (T68 L2 P2-2): a field's lane.v3 signals are
#   (lane.v2 signals - named exemptions) | lane.v3 additions.
# _v2_field_signals() is lane.v2's field rules bit for bit (frozen constants
# _V2_*, line-by-line sentences); tests compare it with the frozen fixture
# tests/fixtures/classify/lane_v2.py. _v3_additions() runs the rules above on
# lane.v3 sentences (joined lines) and can only add. _EXEMPTIONS is the one,
# closed place where a lane.v2 signal may fall, per sentence and only in
# done/notes; each fallen signal is reported as "<field>:exempted" with the
# exemption's name. The narrowing inside _v3_additions only keeps it from
# re-adding a false alarm; it never removes a lane.v2 signal.

CLASSIFIER_VERSION = "lane.v3"


def _folded(text):
    return tuple(_fold(word) for word in text.split())


_A_VERBS = _folded("push publish deploy commit merge rebase tag upload send email mail post")
_A_TOOLS = _folded("curl wget ssh scp rsync kubectl terraform helm twine gh")
_A_HARD = _folded("rm rmtree rmdir unlink shred mkfs truncate purge wipe erase")
_A_SOFT = _folded("delete remove drop sil kaldır temizle boşalt")
_A_CALL_EXTRA = _folded("put patch delete remove urlopen sendmail create_connection")
_A_NET = frozenset(_folded("requests httpx urllib aiohttp smtplib socket boto3 paramiko ftplib"))
_A_GIT = frozenset(_folded("push commit tag merge rebase reset clean"))
_A_PAIRS = (("docker", "push"), ("npm", "publish"), ("pip", "upload"), ("force", "push"),
            ("cargo", "publish"), ("compose", "push"))
_RUN_VERBS = _folded("""run execut exec call invok launch start trigger spawn schedul
python çalıştır koş başlat tetikle""")
# lane.v3 (P2-2): "source" is a run verb only before an identifier or command.
_RUN_SOURCE = _fold("source")
# Interpreter names match as whole words: as prefixes, sh would make every
# "should"/"show"/"share" sentence a call context.
_RUN_WORDS = frozenset(_folded("sh bash zsh node nodejs npx"))
_CALL_EXTENSIONS = (".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd")
_A4_LEADS = frozenset(_folded("also finally please then and afterwards next now"))
_A4_VERBS = frozenset(_A_VERBS)
_A4_TR = _folded("gönder yayınla yayımla yükle paylaş birleştir pushla commitle")
_A4_TR_NEXT = frozenset(_folded("ve sonra"))
_A8_TARGETS = frozenset(_folded("to into onto"))
# lane.v3 (P2-1/P2-2): object, noun-use and negation context for bare verbs.
_DETERMINERS = frozenset(_folded("""the a an this that these those its their our your my his
her one no each every any some another"""))
_OBJECT_NEXT = _DETERMINERS | frozenset(_folded("it them everything all both changes results files"))
_NEGATIONS = frozenset(_folded("not never t nor without"))
_PASSIVE_NEGATIONS = frozenset(_folded("not never nothing no none neither nor"))
_CLAUSE_JOINERS = frozenset(_folded("and but then so while once after when if"))
# T68 r2: the run verb's object noun phrase ends at these words.
_PREPOSITIONS = frozenset(_folded("""of in on at for from by with under over after before into to
onto via about"""))
_NP_BREAKS = _CLAUSE_JOINERS | _PREPOSITIONS
# Words that end exempt:descriptive_subject's prepositional phrase (T68 r2-r4).
_RELATIVES = frozenset(_folded("that which who whose where"))
_SUBORDINATORS = _CLAUSE_JOINERS | frozenset(_folded("""before until unless because as since though
although whenever once so then"""))
# Prepositions a descriptive subject may carry ("Tag names in the parser"); after/before are
# subordinators there.
_DESCRIPTIVE_PREPOSITIONS = _PREPOSITIONS - frozenset(_folded("after before to into onto"))
# T68 r2: Turkish words that are never a dative/locative destination.
_APOSTROPHE_SUFFIX = re.compile(r"[^\s'’]['’]([^\W\d_]+)", re.UNICODE)
_TR_FUNCTION = frozenset(_folded("""ve veya ya da de ile ise ne sonra daha bile yine gibi diye ama fakat
çünkü ancak hala sadece önce şimdi artık"""))
_AUXILIARIES = frozenset(_folded("is are was were be been being get gets got getting"))
_ADVERBS = frozenset(_folded("then also finally now automatically immediately always"))
_ABBREVIATIONS = frozenset(_folded("e.g. i.e. vs. etc."))
_SINGLE_LETTER = re.compile(r"[^\W\d_]\.", re.UNICODE)
# Turkish light verbs after an English verb (push et, commit at, deploy yap).
_A4_TR_LOAN = frozenset(_folded("push commit deploy merge rebase publish upload tag mail email"))
# T68 r4 (Fable P2-1): apostrophe verb suffixes (push'la, merge'le, commit'leyin).
_A4_TR_APOSTROPHE = frozenset(_folded("""la le layın leyin layınız leyiniz layıp leyip lasın lesin
lamalı lemeli"""))
_A4_TR_FORGET = frozenset(_folded("meyi mayı"))
_A4_TR_FORGET_NEXT = _fold("unutma")
_TO_OBJECTS = frozenset(_folded("it them"))
_A4_TR_LIGHT = frozenset(_folded("""et etsin etsinler etmek etmeli etmelisin etmeyi edin ediniz edip
ederek eder edilir edilsin edilecek edildi edilmeli atın atınız atıp atsın atılır atılsın atılacak
atıldı yap yapın yapınız yapıp yapsın yapılır yapılsın yapılacak yapıldı"""))
_A4_TR_AT = _fold("at")
# Suffix + continuation (yükleyip, gönderin, yayınlasın): an action whatever follows.
_A4_TR_GOING = frozenset(_folded("""in un ün iniz unuz ünüz yin yun yün yiniz yunuz yünüz ip up üp
yip yup yüp erek arak yerek yarak sin sun sün"""))
# Turkish passive aorist suffix (yüklenir, birleştirilir), folded.
_A4_TR_DESCRIBE = re.compile(r"^(?:n|[iuü]l)[iuü]r(?:ler|lar)?$")
_A4_TR_LIVE = frozenset(_folded("yayına canlıya"))


def _inflections(verbs):
    forms = set()
    for verb in verbs:
        forms.update(verb + suffix for suffix in ("s", "es", "ed", "d", "ing"))
        if verb.endswith("e"):
            forms.add(verb[:-1] + "ing")
        if verb.endswith("t"):
            forms.update((verb + "ted", verb + "ting"))
    return forms


_A8_FORMS = frozenset(_inflections(_folded(
    "push publish upload deploy send email mail merge rebase commit post tag"))
    | {"sent", "tagged", "tagging"})
# Outward words for part scans: stem prefix or a known inflection (merging).
_OUTWARD_FORMS = _A8_FORMS | _inflections(_A_VERBS)
# lane.v3: bare verbs that count mid-sentence (P2-1) and full prose forms (P2-2).
_A8_BARE = frozenset(_folded("push publish upload deploy send email mail merge rebase commit post tag"))
_OUTWARD_FULL = _OUTWARD_FORMS | frozenset(_A_VERBS)
# Passive state sentences (is pushed); merge/post/tag describe too much else.
_PASSIVE_FORMS = frozenset(w for w in _inflections(_folded(
    "push publish upload deploy send email mail commit rebase")) if w.endswith("ed")) | {"sent"}


def _plurals(nouns):
    forms = set(nouns)
    for noun in nouns:
        forms.update((noun + "s", noun + "es"))
        if noun.endswith("y"):
            forms.add(noun[:-1] + "ies")
    return forms


_A8_NOUNS = frozenset(_plurals(_folded(
    "branch tag commit package wheel release image changes report email message summary artifact"
    " build crate payload")))
_NARROW_RISK = frozenset(_folded("""auth login oauth sso password secret credential apikey
payment billing invoice stripe migration production encrypt pii gdpr kvkk"""))
_NARROW_RISK_PREFIX = _folded("şifre parola ödeme fatura")

# lane.v2 base (T68 r4): the constants lane.v3 changed, frozen at their lane.v2
# values. Every other constant _v2_field_signals reads is unchanged since lane.v2
# (test_classify_v3.V2BaseTests compares them with the frozen fixture).
_V2_A_PAIRS = (("docker", "push"), ("npm", "publish"), ("pip", "upload"), ("force", "push"))
_V2_RUN_VERBS = _folded("""run execut exec call invok launch start trigger spawn schedul
source python çalıştır koş başlat tetikle""")
_V2_A4_TR = _folded("gönder yayınla yükle paylaş birleştir pushla commitle")
_V2_A8_FORMS = frozenset(_inflections(_folded(
    "push publish upload deploy send email mail merge rebase commit")) | {"sent"})
_V2_OUTWARD_FORMS = _V2_A8_FORMS | _inflections(_A_VERBS)
_V2_A8_NOUNS = frozenset(_plurals(_folded(
    "branch tag commit package wheel release image changes report email message summary artifact")))
_V2_BULLET = re.compile(r"^\s*(?:(?:[-*+•]|\d+[.)]|\[[ xX]\])\s+)+")

# T68 r4 exemptions (closed lists; Fable T68 L2 P1-4, P1-5).
_EXEMPT_HEADS = frozenset(_folded("""names messages notifications conflicts requests hooks keys
permissions titles scripts limits fields"""))
_EXEMPT_STATES = frozenset(_folded("is are stay stays remain remains"))
_EXEMPT_BREAKERS = frozenset(_folded("""must will should can may has have done complete completed
happen happens happened"""))
_EXEMPT_TR_PAIRS = frozenset(zip(_folded("birleştir yükle"), _folded("etiketler ayarlar")))
# T68 r5: Turkish conditional suffixes (sağlanırsa, geçerse, varsa, yoksa), folded.
_TR_CONDITIONAL = re.compile(r"(?:r(?:sa|se)|varsa|yoksa)$")
_EXEMPT_TR_TARGETS = frozenset(_folded("ile üzerinden"))

_PLURAL_CALL = re.compile(r"(?<=[^\W_])\((?:s|es|\d+)\)", re.UNICODE | re.IGNORECASE)
_FENCE = re.compile(r"(`{3,})(.*?)\1", re.S)
_DOUBLE = re.compile(r"``(.+?)``", re.S)
_SINGLE = re.compile(r"`([^`]+)`")
# lane.v3 (T68 r4, P1-3): also – — · > and one-letter items (a. b) A.).
_BULLET = re.compile(r"^\s*(?:(?:[-*+•–—·>]|\d+[.)]|[^\W\d_][.)]|\[[ xX]\])\s+)+", re.UNICODE)
# A chained call (x(...).sendmail() keeps its own name after the ").".
_CALLS = re.compile(r"(?:(?<![\w.])|(?<=\)\.))([^\W\d][\w]*(?:\.[^\W\d][\w]*)*)\(", re.UNICODE)
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"|“([^”]*)”|‘([^’]*)’")
_HTTP = re.compile(r"(?<![A-Za-z0-9_])(POST|PUT|PATCH|DELETE)(?![A-Za-z0-9_])")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_SENTENCE_END = ".;!?"
_CLOSERS = "\"'’”)]}*_"


@dataclass(frozen=True)
class _Index:
    paths: frozenset
    basenames: frozenset


def _index(files):
    paths, basenames = set(), set()
    for path in files or ():
        if isinstance(path, str) and path and "\0" not in path:
            key = globs.path_key(path)
            paths.add(key)
            basenames.add(key.rsplit("/", 1)[-1])
    return _Index(frozenset(paths), frozenset(basenames))


# Used by the v2 base (_v2_field_signals); any change must keep V2BaseTests green.
def _pieces(text):
    """K-1: fences, then double, then single backticks; odd leftovers are prose."""
    out = []

    def singles(raw):
        if raw.count("`") % 2:
            out.append(("prose", raw.replace("`", " ")))
            return
        for i, part in enumerate(_SINGLE.split(raw)):
            if i % 2:
                out.append(("cmd" if any(c.isspace() for c in part) else "code", part))
            elif part:
                out.append(("prose", part))

    def doubles(raw):
        last = 0
        for match in _DOUBLE.finditer(raw):
            singles(raw[last:match.start()])
            part = match.group(1).strip()
            out.append(("cmd" if any(c.isspace() for c in part) else "code", part))
            last = match.end()
        singles(raw[last:])

    last = 0
    for match in _FENCE.finditer(text):
        doubles(text[last:match.start()])
        out.append(("fence", match.group(2)))
        last = match.end()
    doubles(text[last:])
    return out


def _ends_sentence(chunk):
    """lane.v3 (P2-4): .;!? end a sentence, except e.g./i.e./vs./etc. and 'x.'."""
    core = chunk.rstrip(_CLOSERS)
    if core[-1:] not in _SENTENCE_END:
        return False
    if core.endswith("."):
        word = _fold(core.lstrip("\"'`’‘“”([{<*_"))
        if word in _ABBREVIATIONS or _SINGLE_LETTER.fullmatch(word):
            return False
    return True


def _capitalized(chunk):
    letters = chunk.lstrip("\"'`’‘“”([{<*_")
    return letters[:1].isupper()


def _sentences(text):
    """Split one field into sentences of (kind, raw) tokens.

    lane.v3 (P2-4): a single line break is a space (managers wrap notes at 80
    columns); a blank line and a bulleted line start a new sentence. T68 r4:
    an abbreviation or one letter plus a dot followed by a capitalized word
    ends the sentence ("etc. Deploy the site."). This only feeds the lane.v3
    additions; lane.v2's line-by-line sentences (_v2_sentences) stay the base.
    """
    sentences, current = [], []
    line_start = True
    abbreviation = False

    def close():
        if current:
            sentences.append(list(current))
            current.clear()

    for kind, raw in _pieces(text):
        if kind == "fence":
            close()
            sentences.append([("cmd", raw)])
            line_start = abbreviation = False
            continue
        if kind != "prose":
            current.append((kind, raw))
            line_start = abbreviation = False
            continue
        lines = raw.split("\n")
        for n, line in enumerate(lines):
            if n:
                line_start = True
                if not line.strip() and n < len(lines) - 1:
                    close()  # blank line
            if line_start:
                bullet = _BULLET.match(line)
                if bullet:
                    close()
                    line = line[bullet.end():]
            for chunk in line.split():
                if abbreviation and _capitalized(chunk):
                    close()
                current.append(("prose", chunk))
                ends = _ends_sentence(chunk)
                abbreviation = not ends and chunk.rstrip(_CLOSERS).endswith(".")
                if ends:
                    close()
            if line.strip():
                line_start = False
    close()
    return sentences


def _v2_sentences(text):
    """lane.v2 sentences, frozen: every line break and every .;!? ends one."""
    sentences, current = [], []
    line_start = True

    def close():
        if current:
            sentences.append(list(current))
            current.clear()

    for kind, raw in _pieces(text):
        if kind == "fence":
            close()
            sentences.append([("cmd", raw)])
            line_start = False
            continue
        if kind != "prose":
            current.append((kind, raw))
            line_start = False
            continue
        lines = raw.split("\n")
        for n, line in enumerate(lines):
            if n:
                close()
                line_start = True
            if line_start:
                line = _V2_BULLET.sub("", line)
            for chunk in line.split():
                current.append(("prose", chunk))
                if chunk.rstrip(_CLOSERS)[-1:] in _SENTENCE_END:
                    close()
            if line.strip():
                line_start = False
    close()
    return sentences


# Used by the v2 base (_v2_field_signals); any change must keep V2BaseTests green.
def _identifier_chunk(chunk):
    core = chunk.strip(_EDGE)
    return bool(core) and bool(_IDENTIFIER_SHAPE.search(core) or _CALL.search(chunk) or _camel(core))


def _resolved(core, index):
    """K-2 (ii): full tracked path, or tracked basename with an extension."""
    while core.startswith("./"):
        core = core[2:]
    core = _LINE_SUFFIX.sub("", core)
    if not core:
        return False
    key = globs.path_key(core)
    if key in index.paths:
        return True
    name = key.rsplit("/", 1)[-1]
    return "/" not in key and "." in name.strip(".") and key in index.basenames


# Used by the v2 base (_v2_field_signals); any change must keep V2BaseTests green.
def _exposed(core, chunk, context, index):
    """Identifier parts an action stem may come from (empty = hidden)."""
    called = context or "(" in chunk or _fold(core).endswith(_CALL_EXTENSIONS)
    if not called and _resolved(core, index):
        return [], []
    if not called and "/" not in core and "." in core:
        components = core.split(".")
        kept = [c for c in components if globs.path_key(c + ".py") not in index.basenames]
        if len(kept) < len(components):
            core = " ".join(kept)
    return _words(core), _camel_words(core)


def _starts(word, stems):
    return any(word.startswith(stem) for stem in stems)


def _outward_word(word):
    return _starts(word, _A_VERBS) or word in _OUTWARD_FORMS


def _command_hits(text, forms=_OUTWARD_FORMS):
    """A1 over a command span (and A2's quoted arguments)."""
    folded = _fold(text)
    words = _words(text)
    hits = set()
    for word in words:
        if _starts(word, _A_VERBS + _A_TOOLS + _A_HARD) or word in forms:
            hits.add(word[:40])
    for first, second in (("drop", "table"), ("alter", "table")):
        if any(a == first and b.startswith(second) for a, b in zip(words, words[1:])):
            hits.add(first + " " + second)
    if "grant" in words and "on" in words[words.index("grant") + 1:]:
        hits.add("grant … on")
    raw = folded.split()
    if "-delete" in raw:
        hits.add("-delete")
    hits.update(_hard_phrases(words, folded))
    return hits


# Used by the v2 base (_v2_field_signals); any change must keep V2BaseTests green.
def _hard_phrases(words, folded):
    hits = set()
    if re.search(r"\breset\s+--hard\b", folded):
        hits.add("reset --hard")
    pairs = list(zip(words, words[1:]))
    for first, second in (("git", "clean"), ("drop", "table"), ("delete", "from")):
        if (first, second) in pairs:
            hits.add(first + " " + second)
    if any(a == "yok" and b.startswith("et") for a, b in pairs):
        hits.add("yok et")
    if "find" in words and "-delete" in folded.split():
        hits.add("find … -delete")
    return hits


def _call_hits(text, forms=_OUTWARD_FORMS):
    """A2: call shapes plus their quoted arguments, scanned like A1."""
    hits = set()
    for match in _CALLS.finditer(text):
        components = [_fold(c) for c in match.group(1).split(".")]
        last, first = components[-1], components[0]
        if (_starts(last, _A_VERBS + _A_HARD + _A_CALL_EXTRA) or last in forms
                or first in _A_TOOLS or first in _A_NET):
            hits.add(".".join(components)[:60])
        depth, end = 0, len(text)
        for i in range(match.end() - 1, len(text)):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                end = i
                break
        quoted = " ".join(next(g for g in m.groups() if g is not None)
                          for m in _QUOTED.finditer(text[match.end():end]))
        if quoted.strip():
            hits.update(_command_hits(quoted, forms))
    return hits


def _call_names(text):
    return [_fold(c) for m in _CALLS.finditer(text) for c in m.group(1).split(".")]


def _bare_verb(previous, before):
    """lane.v3: a bare form after a determiner or a negation is not an action."""
    if previous in _DETERMINERS or previous in _NEGATIONS:
        return False
    return not (previous in _A8_TARGETS and before in _NEGATIONS)


def _descriptive_subject(stream, p):
    """exempt:descriptive_subject (T68 r4, Fable P1-5): "Push notifications stay disabled."

    Closed form only: the verb at p is directly followed by one head noun from
    _EXEMPT_HEADS, then optionally one prepositional phrase (a preposition and
    at most three plain words), then directly is/are/stay(s)/remain(s). A modal
    or has/have, or done/complete/happen anywhere in the sentence, breaks it
    ("Deploy scripts must be idempotent.", "Push branch is done.").
    """
    words = [w if k == "w" else None for k, w in stream]
    if any(w in _EXEMPT_BREAKERS for w in words):
        return False

    def word(k):
        return words[k] if 0 <= k < len(words) else None

    if word(p + 1) not in _EXEMPT_HEADS:
        return False
    k = p + 2
    if word(k) in _DESCRIPTIVE_PREPOSITIONS:
        k += 1
        for _ in range(3):
            current = word(k)
            if current in _EXEMPT_STATES:
                break
            if (current is None or current in _PREPOSITIONS or current in _SUBORDINATORS
                    or current in _RELATIVES or current in _A8_TARGETS):
                return False
            k += 1
    return word(k) in _EXEMPT_STATES


def _tr_destination(before, words, raw):
    """T68 r2: a dative/locative word before the verb or an A8 noun is a destination.

    Conservative: any word ending in -a/-e (kanalında, PyPI'ye -> ye, sunucuya)
    counts, except function words and conditionals (sağlanırsa).
    """
    if any(w in _A8_NOUNS for w in words):
        return True
    # An apostrophe suffix also marks identifiers (PyPI'ye, `main`'e).
    if any(_fold(m.group(1))[-1:] in ("a", "e") for m in _APOSTROPHE_SUFFIX.finditer(raw)):
        return True
    for word in before:
        if word in _TR_FUNCTION or word.endswith(("sa", "se")):
            continue
        if word[-1:] in ("a", "e"):
            return True
    return False


def _tr_conditional_description(words, i, stem, raw):
    """exempt:tr_conditional_description (T68 r4, Fable P1-4).

    The Turkish passive aorist is risky by default ("Rapor Slack ile
    paylaşılır.", "PR birleştirilir."). It describes only in this closed form:
    one exact (stem, object) pair of _EXEMPT_TR_PAIRS (birleştir + etiketler,
    yükle + ayarlar; paylaş never), the object directly before the passive
    aorist, a Turkish conditional (-rsa/-rse, varsa/yoksa) earlier in the
    sentence, no "ile"/"üzerinden" anywhere, and no destination
    ("Koşul sağlanırsa etiketler birleştirilir."). T68 r5: narrowed per L2.
    """
    word = words[i]
    if not _A4_TR_DESCRIBE.match(word[len(stem):]) or i < 1:
        return False
    if (stem, words[i - 1]) not in _EXEMPT_TR_PAIRS:
        return False
    present = [w for w in words if w is not None]
    before = [w for w in words[:i] if w is not None]
    if any(w in _EXEMPT_TR_TARGETS for w in present):
        return False
    if not any(_TR_CONDITIONAL.search(w) for w in words[:i - 1] if w is not None):
        return False
    return not _tr_destination(before, present, raw)


def _passive(stream, i, clause_starts):
    """lane.v3: "is pushed", "are then committed"; negated passives are not actions.

    The subject is searched back (at most 6 tokens) to the clause start, a
    comma/colon or a conjunction: "No plan.json or plan.md is published" is
    negated, "No tests fail and the branch is pushed" is not.
    """
    previous = stream[i - 1][1] if i and stream[i - 1][0] == "w" else None
    if previous in _PASSIVE_NEGATIONS:
        return False
    j = i - 2 if previous in _ADVERBS else i - 1
    if j < 0 or stream[j][0] != "w" or stream[j][1] not in _AUXILIARIES:
        return False
    for k in range(j - 1, max(-1, j - 7), -1):
        kind, word = stream[k]
        if kind == "w" and word in _PASSIVE_NEGATIONS:
            return False
        if k in clause_starts or (kind == "w" and word in _CLAUSE_JOINERS):
            break
    return True


def _v3_additions(field, text, index, risk_map, goal):
    """lane.v3 rules on lane.v3 sentences; only ever added to the lane.v2 base."""
    signals = set()

    def add(rule, detail):
        detail = "".join(c if c.isprintable() else "?" for c in str(detail))[:200]
        signals.add(Signal(field + ":" + rule, detail))

    text = _PLURAL_CALL.sub("", _visible(text))
    for match in _HTTP.finditer(text):
        add("A7", match.group(1))
    for hit in _call_hits(text):
        add("A2", hit)
    for sentence in _sentences(text):
        raw = " ".join(t for _, t in sentence)
        prose, stream, commands = [], [], []
        starts, colon_starts, joined, apostrophes = {0}, set(), set(), set()
        for kind, token in sentence:
            if kind == "prose" and not _identifier_chunk(token):
                words = _words(token)
                folded = _fold(token)
                apostrophes.update(len(stream) + n for n, m in enumerate(_WORD.finditer(folded))
                                   if m.start() and folded[m.start() - 1] in "'’")
                if len(words) > 1 and any(c in token for c in "-/"):
                    joined.update(range(len(stream), len(stream) + len(words)))
                prose.extend(words)
                stream.extend(("w", w) for w in words)
                # lane.v3 (P2-1): "TODO:", "Step 3:" start a sentence, and a
                # comma starts a clause ("After that, push the branch").
                if token.rstrip(_CLOSERS)[-1:] in ":,":
                    colon_starts.add(len(stream))
            elif kind == "cmd":
                commands.append(token)
                stream.append(("cmd", ""))
            else:
                stream.append(("id", ""))
        words = [w for k, w in stream]

        def word_at(i):
            return words[i] if 0 <= i < len(stream) and stream[i][0] == "w" else None

        def has_object(i, end_ok):
            """lane.v3: what follows a verb is an object, a direction or the sentence end."""
            if i + 1 >= len(stream):
                return end_ok
            following = word_at(i + 1)
            return following is None or following in _OBJECT_NEXT or following in _A8_TARGETS

        def run_verb(i):
            word = words[i]
            if word.startswith(_RUN_SOURCE):
                return i + 1 < len(stream) and stream[i + 1][0] != "w"
            return _starts(word, _RUN_VERBS) or word in _RUN_WORDS

        run_positions = [i for i, (kind, _) in enumerate(stream) if kind == "w" and run_verb(i)]
        context = bool(run_positions) or any(_starts(w, _RUN_VERBS) or w in _RUN_WORDS
                                             for w in _call_names(raw))
        exposed, identifiers = [], []
        for kind, token in sentence:
            if kind == "code" or (kind == "prose" and _identifier_chunk(token)):
                core = token if kind == "code" else token.strip(_EDGE)
                plain, camel = _exposed(core, token, context, index)
                exposed.extend(plain)
                identifiers.append(plain)
                identifiers.append(camel)
            elif kind == "prose":
                exposed.extend(_words(token))
        camel_parts = [w for parts in identifiers for w in parts]
        command_words = [w for c in commands for w in _words(c)]
        everything = exposed + camel_parts + command_words

        # A1: command spans.
        for command in commands:
            for hit in _command_hits(command):
                add("A1", hit)
        # A3: command names (exact words / phrases); hidden tracked paths are out.
        for word in exposed + camel_parts:
            if word in _A_TOOLS:
                add("A3", word)
        pairs = set(zip(exposed, exposed[1:]))
        for first, second in pairs:
            if first == "git" and second in _A_GIT:
                add("A3", "git " + second)
        for pair in _A_PAIRS:
            if pair in pairs:
                add("A3", " ".join(pair))
        # A4: imperative at sentence start (after optional leads) or after a lead.
        # lane.v3: after ":" or "," and after a mid-sentence lead the verb needs an
        # object or the sentence end ("pre and post conditions" is not one).
        for start in sorted(starts | colon_starts):
            position = start
            while word_at(position) is not None and words[position] in _A4_LEADS:
                position += 1
            if (word_at(position) in _A4_VERBS and (start not in colon_starts or start == 0
                                                    or has_object(position, True))
                    and not _descriptive_subject(stream, position)):
                add("A4", words[position])
        for i in range(1, len(stream)):
            if (word_at(i) in _A4_VERBS and word_at(i - 1) in _A4_LEADS
                    and has_object(i, True)):
                add("A4", words[i])
        # A4-TR: verb + bounded suffix, then sentence end, ve or sonra.
        for i, (kind, word) in enumerate(stream):
            if kind != "w":
                continue
            following = stream[i + 1] if i + 1 < len(stream) else None
            for stem in _A4_TR:
                if word.startswith(stem) and len(word) - len(stem) <= 8:
                    suffix = word[len(stem):]
                    # lane.v3: suffix + continuation (yükleyip, gönderin) is an action.
                    if suffix in _A4_TR_GOING:
                        add("A4-TR", stem)
                        continue
                    # T68 r4: the same closed form as exempt:tr_conditional_description.
                    if _tr_conditional_description([word_at(k) for k in range(len(stream))],
                                                   i, stem, raw):
                        continue
                    # T68 r4 (Fable P2-1): "yüklemeyi unutma".
                    if suffix in _A4_TR_FORGET and (word_at(i + 1) or "").startswith(_A4_TR_FORGET_NEXT):
                        add("A4-TR", stem)
                        continue
                    if following is None or (following[0] == "w" and following[1] in _A4_TR_NEXT):
                        add("A4-TR", stem)
            # lane.v3 (P2-1): "push et", "commit at", "deploy yap", "yayına al".
            light = word_at(i + 1)
            if word in _A4_TR_LOAN and light is not None:
                if light in _A4_TR_LIGHT:
                    add("A4-TR", word + " " + light)
                elif light == _A4_TR_AT and (i + 2 >= len(stream) or word_at(i + 2) in _A4_TR_NEXT):
                    add("A4-TR", word + " " + light)
            if word in _A4_TR_LIVE and light is not None and light.startswith("al") and len(light) <= 7:
                add("A4-TR", word + " al")
            # T68 r4 (Fable P2-1): "push'la", "Main'e merge'le".
            if word in _A4_TR_LOAN and light in _A4_TR_APOSTROPHE and i + 1 in apostrophes:
                add("A4-TR", word + " " + light)
        # A5: run verb plus an outward or hard destructive part of what is run.
        # lane.v3 (P2-2): identifier and command parts by prefix; a prose word
        # only in full form and only as the run verb's object.
        if context:
            for word in camel_parts + command_words:
                if _outward_word(word) or _starts(word, _A_HARD):
                    add("A5", word[:40])
            # T68 r2: kebab/slash prose parts keep lane.v2's prefix match
            # ("Trigger the mirror-push job").
            for j in sorted(joined):
                word = word_at(j)
                if word is not None and (_outward_word(word) or _starts(word, _A_HARD)):
                    add("A5", word[:40])
            for j, (kind, word) in enumerate(stream):
                if kind != "w" or word not in _OUTWARD_FULL:
                    continue
                for i in run_positions:
                    between = range(i + 1, j)
                    if (i < j and len(between) <= 3
                            and all(word_at(b) is not None and word_at(b) not in _NP_BREAKS
                                    and b not in colon_starts for b in between)
                            and j not in colon_starts):
                        add("A5", word[:40])
        # A6: hard destructive words and phrases.
        for word in everything:
            if word in _A_HARD:
                add("A6", word)
        for hit in _hard_phrases(exposed + command_words, _fold(raw)):
            add("A6", hit)
        # A8: directed verb forms (state / third person sentences).
        for i, (kind, word) in enumerate(stream):
            if kind != "w":
                continue
            near = [w for k, w in stream[i + 1:i + 7] if k == "w"]
            close = [w for k, w in stream[i + 1:i + 4] if k == "w"]
            directed = any(w in _A8_TARGETS for w in near) or any(w in _A8_NOUNS for w in close)
            if word in _A8_FORMS and directed:
                add("A8", word)
            elif (word in _A8_BARE and directed and i not in joined and has_object(i, False)
                  and (i in colon_starts or _bare_verb(word_at(i - 1), word_at(i - 2)))):
                # lane.v3 (P2-1): a bare verb anywhere, with object and direction.
                add("A8", word)
            if word in _PASSIVE_FORMS and _passive(stream, i, colon_starts):
                add("A8", word)
            # T68 r4 (Fable P2-1): "Don't forget to push.", "Make sure to send it."
            if (word in _A8_BARE and word_at(i - 1) == "to" and i not in joined
                    and word_at(i - 2) not in _NEGATIONS
                    and (i + 1 >= len(stream) or word_at(i + 1) in _TO_OBJECTS)):
                add("A8", word)

        if goal:
            # K-2: stems from identifiers that no longer hide them (union with v1).
            prose_words = list(prose)
            for parts in identifiers + [prose_words]:
                for stem in _OUTWARD:
                    if _contains(parts, stem):
                        add("outward_action", stem)
                for stem in _DESTRUCTIVE:
                    if _contains(parts, stem):
                        add("destructive", stem)
        else:
            # K-4: narrow exact-word subset raises; the rest is informational.
            for word in prose:
                if (word in _NARROW_RISK or (word.endswith("s") and word[:-1] in _NARROW_RISK)
                        or _starts(word, _NARROW_RISK_PREFIX)):
                    add("K4", word[:40])
            for stem in risk_map.risky_words:
                if _contains(prose, stem):
                    add("K4", stem)
            for stem in _RISK_WORDS:
                if _contains(prose, stem):
                    add("risk_word", stem)
            for stem in _A_SOFT:
                if _contains(prose, stem):
                    add("destructive", stem)
    return signals


def _v2_field_signals(text, index, risk_map, goal):
    """lane.v2's field rules, bit for bit (T68 r4 base).

    Returns (field-level (rule, detail) set, [(sentence, (rule, detail) set)]).
    The body is the frozen lane.v2 _field_signals with the _V2_* constants;
    only the per-sentence grouping is new, for the exemption pass.
    """
    field_level, per_sentence = set(), []

    def add(target, rule, detail):
        target.add((rule, "".join(c if c.isprintable() else "?" for c in str(detail))[:200]))

    text = _PLURAL_CALL.sub("", _visible(text))
    for match in _HTTP.finditer(text):
        add(field_level, "A7", match.group(1))
    for hit in _call_hits(text, _V2_OUTWARD_FORMS):
        add(field_level, "A2", hit)
    for sentence in _v2_sentences(text):
        found = set()
        per_sentence.append((sentence, found))
        raw = " ".join(t for _, t in sentence)
        prose, stream, commands = [], [], []
        for kind, token in sentence:
            if kind == "prose" and not _identifier_chunk(token):
                words = _words(token)
                prose.extend(words)
                stream.extend(("w", w) for w in words)
            elif kind == "cmd":
                commands.append(token)
                stream.append(("cmd", ""))
            else:
                stream.append(("id", ""))
        context = any(_starts(w, _V2_RUN_VERBS) or w in _RUN_WORDS
                      for w in prose + _call_names(raw))
        exposed, identifiers = [], []
        for kind, token in sentence:
            if kind == "code" or (kind == "prose" and _identifier_chunk(token)):
                core = token if kind == "code" else token.strip(_EDGE)
                plain, camel = _exposed(core, token, context, index)
                exposed.extend(plain)
                identifiers.append(plain)
                identifiers.append(camel)
            elif kind == "prose":
                exposed.extend(_words(token))
        camel_parts = [w for parts in identifiers for w in parts]
        command_words = [w for c in commands for w in _words(c)]
        everything = exposed + camel_parts + command_words

        for command in commands:
            for hit in _command_hits(command, _V2_OUTWARD_FORMS):
                add(found, "A1", hit)
        for word in exposed + camel_parts:
            if word in _A_TOOLS:
                add(found, "A3", word)
        pairs = set(zip(exposed, exposed[1:]))
        for first, second in pairs:
            if first == "git" and second in _A_GIT:
                add(found, "A3", "git " + second)
        for pair in _V2_A_PAIRS:
            if pair in pairs:
                add(found, "A3", " ".join(pair))
        words = [w for k, w in stream]
        for position in _v2_a4_positions(stream):
            add(found, "A4", words[position])
        for i, (kind, word) in enumerate(stream):
            if kind != "w":
                continue
            for stem in _V2_A4_TR:
                if _v2_a4_tr_fires(stream, i, stem):
                    add(found, "A4-TR", stem)
        if context:
            for word in everything:
                if _starts(word, _A_VERBS) or word in _V2_OUTWARD_FORMS or _starts(word, _A_HARD):
                    add(found, "A5", word[:40])
        for word in everything:
            if word in _A_HARD:
                add(found, "A6", word)
        for hit in _hard_phrases(exposed + command_words, _fold(raw)):
            add(found, "A6", hit)
        for i, (kind, word) in enumerate(stream):
            if kind == "w" and word in _V2_A8_FORMS:
                near = [w for k, w in stream[i + 1:i + 7] if k == "w"]
                close = [w for k, w in stream[i + 1:i + 4] if k == "w"]
                if any(w in _A8_TARGETS for w in near) or any(w in _V2_A8_NOUNS for w in close):
                    add(found, "A8", word)

        if goal:
            prose_words = list(prose)
            for parts in identifiers + [prose_words]:
                for stem in _OUTWARD:
                    if _contains(parts, stem):
                        add(found, "outward_action", stem)
                for stem in _DESTRUCTIVE:
                    if _contains(parts, stem):
                        add(found, "destructive", stem)
        else:
            for word in prose:
                if (word in _NARROW_RISK or (word.endswith("s") and word[:-1] in _NARROW_RISK)
                        or _starts(word, _NARROW_RISK_PREFIX)):
                    add(found, "K4", word[:40])
            for stem in risk_map.risky_words:
                if _contains(prose, stem):
                    add(found, "K4", stem)
            for stem in _RISK_WORDS:
                if _contains(prose, stem):
                    add(found, "risk_word", stem)
            for stem in _A_SOFT:
                if _contains(prose, stem):
                    add(found, "destructive", stem)
    return field_level, per_sentence


def _v2_a4_positions(stream):
    """lane.v2 A4: the sentence-start verb after optional leads, and every lead + verb."""
    words = [w for k, w in stream]
    positions = set()
    position = 0
    while position < len(stream) and stream[position][0] == "w" and words[position] in _A4_LEADS:
        position += 1
    if position < len(stream) and stream[position][0] == "w" and words[position] in _A4_VERBS:
        positions.add(position)
    for i in range(1, len(stream)):
        if (stream[i][0] == "w" and words[i] in _A4_VERBS
                and stream[i - 1][0] == "w" and words[i - 1] in _A4_LEADS):
            positions.add(i)
    return positions


def _v2_a4_tr_fires(stream, i, stem):
    """lane.v2 A4-TR: stem + at most 8 more letters, then the end, ve or sonra."""
    word = stream[i][1]
    if stream[i][0] != "w" or not word.startswith(stem) or len(word) - len(stem) > 8:
        return False
    following = stream[i + 1] if i + 1 < len(stream) else None
    return following is None or (following[0] == "w" and following[1] in _A4_TR_NEXT)


def lane_v2_field_signals(field, text, files, risk_map=None, goal=False):
    """The lane.v2 base signals of one field, as lane.v2 named them (for tests)."""
    field_level, per_sentence = _v2_field_signals(text, _index(files), risk_map or RiskMap(), goal)
    found = set(field_level).union(*(signals for _, signals in per_sentence))
    return {Signal(field + ":" + rule, detail) for rule, detail in found}


# --- T68 r4 exemption pass: the only way a lane.v2 signal can fall. ----------
# Each entry is (name, rule, test). A test sees one lane.v2 sentence and one
# signal detail of that rule, and returns True only for its closed form.


def _exemption_view(sentence):
    stream, nonprose = [], set()
    for kind, token in sentence:
        if kind == "prose" and not _identifier_chunk(token):
            stream.extend(("w", w) for w in _words(token))
            continue
        core = token.strip(_EDGE) if kind == "prose" else token
        nonprose.update(_words(core))
        nonprose.update(_camel_words(core))
        stream.append(("cmd" if kind == "cmd" else "id", ""))
    return stream, [w if k == "w" else None for k, w in stream], nonprose


def _exempt_a4(form):
    def test(sentence, detail):
        stream, _, _ = _exemption_view(sentence)
        positions = [p for p in _v2_a4_positions(stream) if stream[p][1] == detail]
        return bool(positions) and all(form(stream, p) for p in positions)
    return test


def _pre_post(stream, p):
    """exempt:prose_pre_post: "Keep the pre and post conditions unchanged."."""
    words = [w if k == "w" else None for k, w in stream]
    following = words[p + 1] if p + 1 < len(words) else None
    return (words[p] == "post" and p >= 2 and words[p - 1] == "and" and words[p - 2] == "pre"
            and following is not None and following not in _OBJECT_NEXT
            and following not in _A8_TARGETS)


def _exempt_tr(sentence, detail):
    stream, words, _ = _exemption_view(sentence)
    raw = " ".join(t for _, t in sentence)
    positions = [i for i in range(len(stream)) if _v2_a4_tr_fires(stream, i, detail)]
    return bool(positions) and all(_tr_conditional_description(words, i, detail, raw)
                                   for i in positions)


# Run-context false alarms: every run verb of the sentence is one of these noun
# or intransitive uses (and no call name is a run verb). T68 r5: each form may
# drop only its own closed list of A5 words; any other A5 word keeps its signal.
_CONTEXT_FORMS = {
    "exempt:prose_model_call": (
        lambda w, i: w[i] in ("call", "calls") and i and w[i - 1] == "model",
        frozenset(_folded("published publishes"))),
    "exempt:prose_source_of_truth": (
        lambda w, i: w[i] == "source" and w[i + 1:i + 3] == ["of", "truth"],
        frozenset(_folded("committed"))),
    "exempt:prose_starts_with": (
        lambda w, i: w[i] in ("starts", "started") and w[i + 1:i + 2] == ["with"],
        frozenset(_folded("tagline postal"))),
    "exempt:prose_starts_final": (
        lambda w, i: w[i] in ("starts", "started") and i == len(w) - 1,
        frozenset(_folded("truncated"))),
}


def _published_outputs(w, i):
    """'published .orcexe outputs': one to three identifiers or 'orcexe' in between (T68 r5)."""
    for k in range(i + 2, min(i + 5, len(w))):
        if w[k] in ("output", "outputs"):
            return all(x is None or x == "orcexe" for x in w[i + 1:k])
    return False


def _publishes_no(w, i):
    """'publishes no plan.json': an identifier or the sentence end after 'no' (T68 r5)."""
    return w[i + 1:i + 2] == ["no"] and (i + 2 >= len(w) or w[i + 2] is None)


# Outward-word false alarms: every prose occurrence of the word has this form.
_WORD_FORMS = {
    "exempt:prose_published_outputs": ("published", _published_outputs),
    "exempt:prose_publishes_no": ("publishes", _publishes_no),
}


def _exempt_a5(sentence, detail):
    """A5 prose words only: a word that is also an identifier/command part never falls."""
    stream, words, nonprose = _exemption_view(sentence)
    if detail in nonprose or detail not in words:
        return None
    for name, (form_word, form) in _WORD_FORMS.items():
        occurrences = [i for i, w in enumerate(words) if w == form_word]
        if detail == form_word and all(form(words, i) for i in occurrences):
            return name
    raw = " ".join(t for _, t in sentence)
    if any(_starts(w, _V2_RUN_VERBS) or w in _RUN_WORDS for w in _call_names(raw)):
        return None
    sources = [i for i, w in enumerate(words)
               if w is not None and (_starts(w, _V2_RUN_VERBS) or w in _RUN_WORDS)]
    names = set()
    for i in sources:
        name = next((n for n, (form, allowed) in _CONTEXT_FORMS.items()
                     if form(words, i) and detail in allowed), None)
        if name is None:
            return None
        names.add(name)
    return "+".join(sorted(names)) or None


_EXEMPTIONS = (
    ("exempt:descriptive_subject", "A4", _exempt_a4(_descriptive_subject)),
    ("exempt:prose_pre_post", "A4", _exempt_a4(_pre_post)),
    ("exempt:tr_conditional_description", "A4-TR", _exempt_tr),
)
EXEMPTION_NAMES = frozenset([name for name, _, _ in _EXEMPTIONS] + list(_CONTEXT_FORMS)
                            + list(_WORD_FORMS))


def _exemption(sentence, rule, detail):
    """The exemption name that drops this lane.v2 signal in this sentence, or None."""
    if rule == "A5":
        return _exempt_a5(sentence, detail)
    for name, exempt_rule, test in _EXEMPTIONS:
        if rule == exempt_rule and test(sentence, detail):
            return name
    return None


def _field_signals(field, text, index, risk_map, goal):
    """(lane.v2 signals - named exemptions) | lane.v3 additions, for one field.

    Exemptions apply to done/notes only; the goal keeps every lane.v2 signal.
    A signal some exemption dropped in every sentence that raised it, and that
    no lane.v3 rule raises again, is reported as '<field>:exempted' with the
    detail '<exemption name> <rule>=<detail>'.
    """
    field_level, per_sentence = _v2_field_signals(text, index, risk_map, goal)
    kept, dropped = set(field_level), {}
    for sentence, found in per_sentence:
        for rule, detail in found:
            name = None if goal else _exemption(sentence, rule, detail)
            if name is None:
                kept.add((rule, detail))
            else:
                dropped.setdefault((rule, detail), set()).add(name)
    signals = {Signal(field + ":" + rule, detail) for rule, detail in kept}
    signals |= _v3_additions(field, text, index, risk_map, goal)
    for (rule, detail), names in dropped.items():
        if Signal(field + ":" + rule, detail) not in signals:
            for name in names:
                note = name + " " + rule + "=" + detail
                signals.add(Signal(field + ":exempted",
                                   "".join(c if c.isprintable() else "?" for c in note)[:200]))
    return signals


_FIELD_RISKY = frozenset("A1 A2 A3 A4 A4-TR A5 A6 A7 A8 K4 outward_action".split())


def task_lane(goal: str, done, notes, files: Sequence[str] | None,
              risk_map: RiskMap | None = None) -> Classification:
    """lane.v3: max of the goal lane and each done item / notes lane.

    The goal keeps every lane.v1 rule (classify()) and adds K-2/K-3 signals,
    so no goal can get a lower lane than classify(goal, files). Done items and
    notes use A1-A8 and the narrow K-4 risk words; signals name their field
    (goal:A2, done[2]:A8, notes:K4). Soft destructive words only floor the
    lane at standard; informational risk words never change it.
    """
    if not isinstance(goal, str):
        raise TypeError("goal must be a string")
    if isinstance(done, (str, bytes)) or not isinstance(done, Sequence):
        raise TypeError("done must be a sequence of strings")
    if any(not isinstance(item, str) for item in done):
        raise TypeError("done must contain strings")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise TypeError("notes must be a string or None")
    base = classify(goal, files, risk_map)
    risk_map = risk_map or RiskMap()
    index = _index(files)
    fields = [("goal", goal, True)] + [("done[{}]".format(i), item, False) for i, item in enumerate(done)]
    if notes:
        fields.append(("notes", notes, False))
    existing = {(s.id, s.detail) for s in base.signals}
    extra = set()
    for name, text, is_goal in fields:
        for found in _field_signals(name, text, index, risk_map, is_goal):
            rule = found.id.split(":", 1)[1]
            # Goal stems v1 already reported stay under their v1 id only.
            if is_goal and rule in ("outward_action", "destructive") and (rule, found.detail) in existing:
                continue
            extra.add(found)
    lane = base.lane
    if any(s.id.split(":", 1)[1] in _FIELD_RISKY for s in extra):
        lane = "risky"
    elif lane == "fast" and any(s.id.split(":", 1)[1] == "destructive" for s in extra):
        lane = "standard"
    signals = tuple(sorted(set(base.signals) | extra, key=lambda s: (s.id, s.detail)))
    return Classification(base.mode, lane, base.targets, signals)


def _blocked(result, name, floor="standard"):
    """An unavailable input never lowers the text lane and never grants fast.

    T58 (P1-3): this used to turn a text-derived risky lane into standard when
    the local risk map was unusable; every unavailable input is now a floor.
    Decision 028 (T68): an unusable risk map floors at risky, because the
    project's own risky words and paths are unknown (git_unavailable stays
    standard: it only covers the map, see repository_risk_map callers).
    """
    signals = tuple(sorted(result.signals + (Signal(name, "unavailable"),),
                           key=lambda s: (s.id, s.detail)))
    lane = "risky" if result.lane == "risky" or floor == "risky" else "standard"
    return Classification(result.mode, lane,
                          result.targets, signals)


# Any unusable local map (corrupt, wrong type, unknown key, unsupported version,
# dangling/looping symlink, unreadable) is the same fail-closed floor.
_RISK_MAP_ERRORS = (ValueError, OSError, TypeError, RecursionError)


def repository_risk_map(root):
    """Load the same main-worktree additions for do and plan.

    Decision 028 (T68): a symlinked map is unusable even when its target
    parses; the map must be the repository's own file.
    """
    path = Path(root) / '.orcexe' / 'risk-map.yaml'
    if path.is_symlink():
        raise ValueError('risk_map_invalid')
    try:
        return load_risk_map(path)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError('risk_map_invalid')
        return RiskMap()


def classify_repo(workdir: str, task: str) -> Classification:
    try:
        if not gitutil.is_repo(workdir):
            return classify(task, None)
        root = gitutil.main_worktree(workdir)
        files = gitutil.ls_files(root)
    except gitutil.GitError:
        return _blocked(classify(task, None), "git_unavailable")
    try:
        risk_map = repository_risk_map(root)
    except _RISK_MAP_ERRORS:
        return _blocked(classify(task, files), "risk_map_invalid", "risky")
    return classify(task, files, risk_map)


def task_lane_repo(workdir: str, goal: str, done, notes) -> Classification:
    """task_lane with the same repository inputs and failure floors as classify_repo."""
    try:
        if not gitutil.is_repo(workdir):
            return task_lane(goal, done, notes, None)
        root = gitutil.main_worktree(workdir)
        files = gitutil.ls_files(root)
    except gitutil.GitError:
        return _blocked(task_lane(goal, done, notes, None), "git_unavailable")
    try:
        risk_map = repository_risk_map(root)
    except _RISK_MAP_ERRORS:
        return _blocked(task_lane(goal, done, notes, files), "risk_map_invalid", "risky")
    return task_lane(goal, done, notes, files, risk_map)
