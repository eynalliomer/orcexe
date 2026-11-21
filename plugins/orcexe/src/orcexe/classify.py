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


def _blocked(result, name):
    signals = tuple(sorted(result.signals + (Signal(name, "unavailable"),),
                           key=lambda s: (s.id, s.detail)))
    lane = "risky" if result.lane == "risky" and name != "risk_map_invalid" else "standard"
    return Classification(result.mode, lane,
                          result.targets, signals)


def repository_risk_map(root):
    """Load the same main-worktree additions for do and plan."""
    path = Path(root) / '.orcexe' / 'risk-map.yaml'
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
    except (ValueError, OSError):
        return _blocked(classify(task, files), "risk_map_invalid")
    return classify(task, files, risk_map)
