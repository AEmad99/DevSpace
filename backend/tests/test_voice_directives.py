"""Voice-directive parser tests for the audio generation pipeline.

Covers:
  * gender / age / explicit-voice-id aliases in ``parse_voice_directives``
  * emotion keyword extraction
  * tone (pitch / intensity / timbre) extraction
  * cleanup of framing words ("a man's voice saying", "in a deep voice")
  * safe defaults (returns ``None`` and the original text when no directive
    is found — never silently rewrites the spoken text)
  * ``_parse_intent_json`` 5-tuple shape (kind, prompt, voice, emotion, vm)
  * ``minimax_audio_events`` payload shape — voice_modify only sent when set,
    emotion only when set, default voice_id is a real MiniMax id.
"""

import asyncio
import json
import sys
from pathlib import Path

# Make ``backend`` importable when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


from src.model_media import (
    _parse_intent_json,
    minimax_audio_events,
    parse_voice_directives,
)


# ── parse_voice_directives ────────────────────────────────────────────────────

def test_gender_man_picks_male_voice_and_strips_framing():
    clean, voice, emotion, vm = parse_voice_directives(
        "generate a man's voice saying 'today we are going to talk about devspace'"
    )
    assert voice == "English_ManWithDeepVoice"
    assert emotion is None
    assert vm is None
    assert "man" not in clean.lower()
    assert "voice" not in clean.lower()
    assert "today we are going to talk about devspace" in clean.lower()


def test_gender_woman_picks_female_voice():
    clean, voice, emotion, vm = parse_voice_directives(
        "say 'hello there' in a woman's voice"
    )
    assert voice == "English_Graceful_Lady"
    assert "hello there" in clean.lower()


def test_gender_girl_young_variant():
    clean, voice, _, _ = parse_voice_directives("read this in a girl's voice: good morning")
    assert voice == "English_radiant_girl"
    assert "good morning" in clean.lower()


def test_age_elderly_man():
    _, voice, _, _ = parse_voice_directives("narrate this with an old man's voice: once upon a time")
    assert voice == "English_Deep-VoicedGentleman"


def test_explicit_voice_id_wins_over_gender():
    """User can name a specific voice id (case-insensitive); canonical case is returned."""
    clean, voice, _, _ = parse_voice_directives(
        "use English_CompellingLady1 voice to say 'welcome back'"
    )
    assert voice == "English_compelling_lady1"  # canonical casing from the catalogue
    assert "welcome back" in clean.lower()


def test_unknown_word_is_not_treated_as_voice_id():
    """A random word like 'the' must NOT be picked as a voice_id."""
    _, voice, _, _ = parse_voice_directives("say hello in a friendly way")
    assert voice is None


def test_emotion_happy():
    clean, voice, emotion, vm = parse_voice_directives(
        "say 'good morning everyone' in a happy cheerful voice"
    )
    assert emotion == "happy"
    assert voice is None
    assert "good morning everyone" in clean.lower()


def test_emotion_angry_does_not_strip_substring_words():
    """'angry' must match as a word, not eat substrings of unrelated words."""
    clean, _, emotion, _ = parse_voice_directives("say 'the package arrived' angrily")
    assert emotion == "angry"
    assert "the package arrived" in clean.lower()


def test_tone_deep_sets_pitch():
    _, _, _, vm = parse_voice_directives("say 'over there' in a deep voice")
    assert vm and vm.get("pitch") == -50


def test_tone_soft_sets_intensity():
    _, _, _, vm = parse_voice_directives("say 'goodnight' softly")
    assert vm and vm.get("intensity") == 50


def test_tone_loud_sets_intensity_negative():
    _, _, _, vm = parse_voice_directives("say 'fire!' loudly")
    assert vm and vm.get("intensity") == -50


def test_tone_crisp_sets_timbre():
    _, _, _, vm = parse_voice_directives("say 'attention please' in a crisp voice")
    assert vm and vm.get("timbre") == 30


def test_combined_voice_emotion_tone():
    """All three axes at once — woman, happy, soft."""
    clean, voice, emotion, vm = parse_voice_directives(
        "say 'welcome back dear' in a woman's voice, happy and softly"
    )
    assert voice == "English_Graceful_Lady"
    assert emotion == "happy"
    assert vm and vm.get("intensity") == 50
    assert "welcome back dear" in clean.lower()


def test_no_directive_returns_none_and_preserves_text():
    """A plain read-aloud request with no voice spec must come back unchanged."""
    raw = "read 'hello world' aloud"
    clean, voice, emotion, vm = parse_voice_directives(raw)
    assert voice is None
    assert emotion is None
    assert vm is None
    # Text is preserved (whitespace may be tidied but content stays).
    assert "hello world" in clean.lower()


def test_empty_input_safe():
    clean, voice, emotion, vm = parse_voice_directives("")
    assert clean == ""
    assert voice is None and emotion is None and vm is None


def test_using_dative_phrase_with_a_voice():
    """`with a woman's voice` (not 'in') should still strip."""
    _, voice, _, _ = parse_voice_directives("narrate 'once upon a time' with a woman's voice")
    assert voice == "English_Graceful_Lady"


def test_low_does_not_match_allow_or_below():
    """`lowly`/`allow` must NOT trigger pitch=-50; only the bare word `low`."""
    _, _, _, vm = parse_voice_directives("say 'do not allow this' in a regular voice")
    assert vm is None or "pitch" not in vm


# ── _parse_intent_json ────────────────────────────────────────────────────────

def test_intent_json_returns_5tuple():
    kind, prompt, voice, emotion, vm = _parse_intent_json(
        '{"action":"audio","prompt":"hello there","voice":"English_ManWithDeepVoice",'
        '"emotion":"happy","voice_modify":{"pitch":-50,"intensity":50,"timbre":0}}'
    )
    assert kind == "audio"
    assert prompt == "hello there"
    assert voice == "English_ManWithDeepVoice"
    assert emotion == "happy"
    assert vm == {"pitch": -50, "intensity": 50, "timbre": 0}


def test_intent_json_filters_voice_modify_axes():
    """Only pitch/intensity/timbre are kept; random keys are dropped."""
    _, _, _, _, vm = _parse_intent_json(
        '{"action":"audio","prompt":"x","voice_modify":{"pitch":-50,"speed":99,"bogus":1}}'
    )
    assert vm == {"pitch": -50}


def test_intent_json_empty_vm_is_none():
    _, _, _, _, vm = _parse_intent_json('{"action":"audio","prompt":"x","voice_modify":{}}')
    assert vm is None


def test_intent_json_non_audio_action_returns_none():
    """image/video/music kind should still parse but voice fields stay None."""
    kind, prompt, voice, _, _ = _parse_intent_json('{"action":"image","prompt":"a cat"}')
    assert kind == "image"
    assert prompt == "a cat"
    assert voice is None


def test_intent_json_chat_action_returns_none_kind():
    kind, *_ = _parse_intent_json('{"action":"chat","prompt":"hi"}')
    assert kind is None


# ── minimax_audio_events payload shape ───────────────────────────────────────

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.last_url = url
        self.last_body = json
        return _FakeResp(self._payload, status=self._status)


def _patch_client(monkeypatch, payload, status=200):
    import src.model_media as mm
    client = _FakeClient(payload, status=status)

    class _Factory:
        def __init__(self, c):
            self.c = c

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *a):
            return False

    def _factory(timeout=None):
        return _Factory(client)

    monkeypatch.setattr(mm.httpx, "AsyncClient", _factory)
    return client


async def _drain(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


def test_audio_default_uses_real_voice_id(monkeypatch):
    """Default voice id must be a valid MiniMax system voice, not 'Wise_Woman'."""
    payload = {"data": {"audio": "deadbeef"}, "base_resp": {"status_code": 0}}
    client = _patch_client(monkeypatch, payload)
    asyncio.run(_drain(minimax_audio_events(
        "https://api.minimax.io", {"Authorization": "Bearer k"},
        "speech-02-hd", "hello world",
    )))
    vid = client.last_body["voice_setting"]["voice_id"]
    assert vid != "Wise_Woman"
    # English_expressive_narrator is the new default we set.
    assert vid == "English_expressive_narrator"


def test_audio_passes_emotion(monkeypatch):
    payload = {"data": {"audio": "deadbeef"}, "base_resp": {"status_code": 0}}
    client = _patch_client(monkeypatch, payload)
    asyncio.run(_drain(minimax_audio_events(
        "https://api.minimax.io", {}, "speech-02-hd", "hi",
        voice="English_ManWithDeepVoice", emotion="happy",
    )))
    assert client.last_body["voice_setting"]["emotion"] == "happy"
    assert client.last_body["voice_setting"]["voice_id"] == "English_ManWithDeepVoice"


def test_audio_passes_voice_modify_only_when_set(monkeypatch):
    payload = {"data": {"audio": "deadbeef"}, "base_resp": {"status_code": 0}}
    client = _patch_client(monkeypatch, payload)
    asyncio.run(_drain(minimax_audio_events(
        "https://api.minimax.io", {}, "speech-02-hd", "hi",
        voice="English_ManWithDeepVoice", emotion="angry",
        voice_modify={"pitch": -50, "intensity": 50},
    )))
    vm = client.last_body.get("voice_modify")
    assert vm == {"pitch": -50, "intensity": 50}
    assert client.last_body["voice_setting"]["emotion"] == "angry"


def test_audio_omits_voice_modify_when_none(monkeypatch):
    payload = {"data": {"audio": "deadbeef"}, "base_resp": {"status_code": 0}}
    client = _patch_client(monkeypatch, payload)
    asyncio.run(_drain(minimax_audio_events(
        "https://api.minimax.io", {}, "speech-02-hd", "hi",
        voice="English_Graceful_Lady",
    )))
    assert "voice_modify" not in client.last_body


def test_audio_omits_emotion_when_none(monkeypatch):
    payload = {"data": {"audio": "deadbeef"}, "base_resp": {"status_code": 0}}
    client = _patch_client(monkeypatch, payload)
    asyncio.run(_drain(minimax_audio_events(
        "https://api.minimax.io", {}, "speech-02-hd", "hi",
        voice="English_Graceful_Lady",
    )))
    assert "emotion" not in client.last_body["voice_setting"]


# ── Meta-instruction stripping (regression for the burger-voice bug) ─────────
#
# These tests cover the case where the LLM classifier returns the raw user
# message verbatim (or with only a partial clean). Before this fix, phrases
# like "generate an audio of" or "should be excited while he says it" reached
# the T2A model and got read aloud literally. The fix is in
# ``_strip_meta_framing`` (called as step 0 of ``parse_voice_directives``) and
# in the chat-routes fallback that now always uses the regex-cleaned text.

def test_meta_strip_extracts_quoted_segment_ignoring_wrap():
    """The original repro: user wraps 'i love burgers' in a long instruction.
    The spoken text must be just the quoted segment."""
    raw = ("generate an audio of a man saying the following: "
           "'i love burgers' should be excited while he says it")
    clean, voice, emotion, _ = parse_voice_directives(raw)
    assert clean == "i love burgers", f"expected the quoted segment, got {clean!r}"
    assert voice == "English_ManWithDeepVoice"
    assert emotion == "happy"  # "excited" → happy


def test_meta_strip_double_quoted_segment():
    clean, _, _, _ = parse_voice_directives(
        'generate an audio saying "the quick brown fox jumps"')
    assert clean == "the quick brown fox jumps"


def test_meta_strip_french_quotes():
    clean, _, _, _ = parse_voice_directives(
        "generate a voice reading « bonjour le monde » in a happy tone")
    assert clean == "bonjour le monde"


def test_meta_strip_leading_imperative_no_quotes():
    """Without quotes, the imperative + wrap + trailing direction are stripped."""
    clean, voice, emotion, _ = parse_voice_directives(
        "please read take a deep breath in a calm voice")
    # 'read' may consume the verb but the spoken content 'take a deep breath'
    # should remain (or close to it).
    assert "take a deep breath" in clean.lower()
    assert emotion == "calm"


def test_meta_strip_saying_the_following_no_quotes():
    """'saying the following' wrap without quotes must be stripped."""
    clean, _, emotion, _ = parse_voice_directives(
        "create a voice saying the following: hello there, with enthusiasm")
    assert "hello there" in clean.lower()
    assert "enthusiasm" not in clean.lower()
    assert "saying the following" not in clean.lower()


def test_meta_strip_should_be_excited_trailing():
    """Trailing 'should be X while he says it' is dropped."""
    clean, _, emotion, _ = parse_voice_directives(
        "generate audio of 'welcome' should be excited while he says it")
    assert clean == "welcome"
    assert emotion == "happy"


def test_meta_strip_tone_in_a_happy_tone():
    clean, _, emotion, _ = parse_voice_directives(
        "say 'over there' in a happy tone")
    assert clean == "over there"
    assert emotion == "happy"


def test_meta_strip_with_enthusiasm():
    clean, _, emotion, _ = parse_voice_directives(
        "generate 'hello world' with enthusiasm")
    assert clean == "hello world"
    # 'enthusiasm' is in the happy synonym list.
    assert emotion == "happy"


def test_meta_strip_where_he_says():
    clean, voice, _, _ = parse_voice_directives(
        "create audio of a man where he says 'howdy partner'")
    assert clean == "howdy partner"
    assert voice == "English_ManWithDeepVoice"


def test_meta_strip_preserves_clean_input():
    """A prompt that needs no stripping comes back unchanged (modulo whitespace)."""
    raw = "i love burgers"
    clean, _, _, _ = parse_voice_directives(raw)
    assert clean == "i love burgers"


def test_meta_strip_preserves_clean_with_voice_directive():
    """A prompt that's already clean (only has voice directive) stays clean."""
    clean, voice, _, _ = parse_voice_directives(
        "say hello in a man's voice")
    assert voice == "English_ManWithDeepVoice"
    # 'hello' survives; the leading 'say' verb is dropped, 'man' is dropped,
    # the 'in a X voice' framing is dropped.
    assert "hello" in clean.lower()
    assert "man" not in clean.lower()
    assert "voice" not in clean.lower()


def test_meta_strip_empty_input_safe():
    clean, voice, emotion, vm = parse_voice_directives("")
    assert clean == ""
    assert voice is None and emotion is None and vm is None


def test_meta_strip_none_input_safe():
    clean, voice, emotion, vm = parse_voice_directives(None)
    assert clean == ""
    assert voice is None and emotion is None and vm is None


def test_meta_strip_combined_directions_inside_quote():
    """Even inside the quoted segment, voice directives on the WRAP are
    extracted from the original (pre-strip) text — e.g. 'an excited man' on
    the wrap sets emotion+voice even though only the quoted words reach TTS."""
    raw = "generate an excited man's voice saying 'wake up!'"
    clean, voice, emotion, _ = parse_voice_directives(raw)
    assert clean == "wake up!"
    assert voice == "English_ManWithDeepVoice"
    assert emotion == "happy"


def test_meta_strip_only_speaking_clause():
    """'a calm voice speaking' wrap with no quoted content strips cleanly."""
    clean, _, emotion, _ = parse_voice_directives(
        "a calm voice speaking take it easy")
    # The content words survive the wrap strip; the verb "speaking" is dropped.
    assert "take it easy" in clean.lower()
    assert "speaking" not in clean.lower()
    assert emotion == "calm"
