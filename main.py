import os, sys, io, json, threading, hashlib, base64, math, time, struct, wave, re, zipfile, shutil
import traceback
import urllib.request, urllib.parse, urllib.error

def _log(msg):
    line = f"[LEMUS {time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open("startup_debug.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

_log("=== STARTUP v7.6.0 ULTIMATE RESTORE ===")

try:
    import certifi, ssl
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())
    _log("SSL: certifi подключён")
except Exception as e:
    _log(f"SSL patch пропущен: {e}")

try:
    from kivy.lang import Builder
    from kivy.utils import platform
    from kivy.core.audio import SoundLoader
    from kivy.clock import Clock
    from kivy.metrics import dp
    from kivy.animation import Animation
    from kivy.uix.widget import Widget
    from kivy.uix.image import Image
    from kivy.properties import NumericProperty, StringProperty, BooleanProperty, ListProperty, ObjectProperty
    from kivy.factory import Factory
    from kivy.logger import Logger
    _log("OK kivy")
except Exception as e:
    _log(f"FAIL kivy: {e}")
    raise

try:
    from kivymd.app import MDApp
    from kivymd.uix.boxlayout import MDBoxLayout
    from kivymd.uix.scrollview import MDScrollView
    from kivymd.uix.toolbar import MDTopAppBar
    from kivymd.uix.bottomnavigation import MDBottomNavigation, MDBottomNavigationItem
    from kivymd.uix.card import MDCard
    from kivymd.uix.textfield import MDTextField
    from kivymd.uix.button import MDRaisedButton, MDIconButton, MDFlatButton
    from kivymd.uix.dialog import MDDialog
    from kivymd.uix.filemanager import MDFileManager
    from kivymd.uix.label import MDLabel
    from kivymd.uix.progressbar import MDProgressBar
    from kivymd.uix.list import (MDList, TwoLineAvatarIconListItem, IconLeftWidget,
                                 IconRightWidget, CheckboxLeftWidget)
    from kivymd.toast import toast
    
    # Fallbacks for separators/switches (FIXED SYNTAX)
    try:
        from kivymd.uix.divider import MDSeparator
    except ImportError:
        try:
            from kivymd.uix.separator import MDSeparator
        except ImportError:
            class MDSeparator(MDBoxLayout):
                def __init__(self, **kwargs):
                    kwargs.setdefault('size_hint_y', None)
                    kwargs.setdefault('height', '1dp')
                    kwargs.setdefault('md_bg_color', [0.26, 0.24, 0.36, 1])
                    super().__init__(**kwargs)
            Factory.register('MDSeparator', cls=MDSeparator)

    try:
        from kivymd.uix.switch import MDSwitch
    except ImportError:
        try:
            from kivymd.uix.selectioncontrol import MDSwitch
        except ImportError:
            try:
                from kivymd.uix.selection import MDSwitch
            except ImportError:
                from kivy.uix.togglebutton import ToggleButton
                from kivy.properties import BooleanProperty
                class MDSwitch(ToggleButton):
                    active = BooleanProperty(False)
                    def __init__(self, **kwargs):
                        super().__init__(**kwargs)
                        self.bind(state=self._on_state)
                    def _on_state(self, instance, value):
                        self.active = (value == 'down')
                Factory.register('MDSwitch', cls=MDSwitch)
    
    _log("OK kivymd")
except Exception as e:
    _log(f"FAIL kivymd: {e}")
    raise

CURRENT_VERSION = "7.6.0"
CONFIG_FILE = "lemus_studio_config.json"
PROJECTS_FILE = "lemus_projects_db.json"
HISTORY_FILE = "lemus_prompts_history.json"
ONBOARDING_FLAG = "onboarding_seen_v7"
MASTER_KEYWORD = "LemusAI"
MASTER_HASH = hashlib.sha256(MASTER_KEYWORD.encode()).hexdigest()
GITHUB_REPO = "antonlemus/lemus-ai-music-apk"
REMOTE_KEYS_URL = "https://gist.githubusercontent.com/antonlemus/YOUR_GIST_ID/raw/keys.json"

GEMINI_MODELS = [
    "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.0-flash-exp",
    "gemini-2.5-pro", "gemini-1.5-flash-latest", "gemini-1.5-pro-latest",
]
OR_MODELS = [
    "qwen/qwen3.8-27b:free", "openrouter/auto",
    "meta-llama/llama-3.3-70b-instruct:free", "deepseek/deepseek-chat:free"
]

REPLICATE_MUSIC_MODELS = [
    {"owner_model": "meta/musicgen",
     "extra": {"model_version": "stereo-large", "output_format": "mp3",
               "normalization_strategy": "peak"}},
    {"owner_model": "meta/musicgen",
     "extra": {"model_version": "melody-large", "output_format": "mp3",
               "normalization_strategy": "peak"}},
    {"owner_model": "meta/musicgen",
     "extra": {"model_version": "large", "output_format": "mp3",
               "normalization_strategy": "peak"}},
]

EMPTY_CONFIG = {
    "gemini_keys": [], "gemini_key": "", "gemini_model": "gemini-2.0-flash",
    "openrouter_key": "", "yandex_token": "", "hf_token": "", "fish_key": "", "groq_key": "",
    "replicate_token": "",
    "is_master_activated": False, "activated_at": None, "activation_source": None,
    "disclose_ai": True, "hints_seen": {},
}

HINTS = {
    "tab_studio": "Студия: выбери режим чипсами сверху. «Улучшить промпт» допишет идею до профессионального описания.",
    "tab_projects": "Медиатека: слушай, скачивай, отправляй и экспортируй готовые треки.",
    "tab_settings": "Настройки: загрузи keys.json для полной мощности. Диагностика покажет точные коды ошибок.",
    "mode_single": "Сингл: тема + жанр + длительность. Демоголос включает клон твоего голоса.",
    "mode_prompt": "Промпт: опиши трек словами или нажми «Улучшить промпт» — студия добавит жанр, BPM, настроение и структуру.",
    "mode_viral": "Хит: мемная фраза станет припевом короткого трека для Reels/TikTok.",
    "mode_album": "Альбом: концепция + жанр → 3-4 трека в едином стиле. Позже добавляй треки кнопкой «Трек в альбом».",
    "mode_money": "Фон: ниша для стримингов → монетизируемый фоновый трек.",
}

TYPO_MAP = [
    ("drumm and base", "drum and bass"), ("drum and base", "drum and bass"),
    ("drumm-n-base", "drum and bass"), ("драм-н-бэйс", "drum and bass"),
    ("drum'n'bass", "drum and bass"), ("медодичный", "мелодичный"),
    ("лоу-фай", "lo-fi"), ("лоуфай", "lo-fi"),
    ("фонк", "phonk"), ("хаус", "house"), ("техно", "techno"),
]

GENRE_BPM = [
    (("drum and bass", "dnb"), 174, "melodic drum and bass", "тёплые пэды, роллинг-бас, брейкбит"),
    (("phonk",), 132, "drift phonk", "ковбелл-мелодия, 808-бас, тёмный вайб"),
    (("lo-fi",), 82, "lo-fi chill", "виниловый шум, родес-пиано, мягкий бит"),
    (("house",), 124, "deep house", "грув-бас, мягкие клавиши, четырёхдольный бит"),
    (("trap", "rap", "рэп", "hip-hop"), 140, "trap rap", "808-бас, хэты с трещоткой, мрачные синты"),
    (("ballad", "баллад"), 72, "pop ballad", "фортепиано, струнные, живой бас"),
    (("techno",), 128, "techno", "индустриальные синты, жёсткий бит"),
    (("pop", "поп"), 100, "modern pop", "чистый продакшн, синтезаторные пэды, живой бас"),
]

def _local_enhance(raw):
    t = (raw or "").lower()
    for a, b in TYPO_MAP:
        t = t.replace(a, b)
    bpm, genre, instr = 100, "modern pop", "тёплые пэды, мягкие ударные, глубокий бас"
    for keys, b_, g_, i_ in GENRE_BPM:
        if any(k in t for k in keys):
            bpm, genre, instr = b_, g_, i_
            break
    vocals = ""
    if any(w in t for w in ("female", "женск")):
        vocals = "женский вокал"
    elif any(w in t for w in ("male", "мужск")):
        vocals = "мужской вокал"
    if any(w in t for w in ("мягк", "soft", "груст", "sad", "лирич", "нежн")):
        mood = "настроение светлой грусти"
    elif any(w in t for w in ("энергич", "агрессив", "драйв", "energy")):
        mood = "энергичное и драйвовое настроение"
    else:
        mood = "тёплое обволакивающее настроение"
    parts = [genre]
    if vocals:
        parts.append(vocals)
    parts += [instr, f"{bpm} bpm", mood]
    head = ", ".join(parts).capitalize()
    return (f"{head}. Структура: короткое интро, длинный куплет, мощный дроп-припев, "
            f"бридж, финальный припев с фейдом.")

ENHANCE_SYSTEM = (
    "Ты — промпт-инженер музыкальных нейросетей уровня Suno/Udio. "
    "Пользователь даёт черновую идею. Ты возвращаешь ОДНУ строку готового промпта на русском: "
    "жанр и поджанр, вокал, инструменты, темп в bpm, тональность, настроение, "
    "структура (интро/куплет/припев/бридж/аутро). Без JSON, без пояснений."
)

_STORAGE_ROOT = None

def _android_private():
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        if activity:
            d = activity.getExternalFilesDir(None)
            if d:
                return os.path.join(d.getAbsolutePath(), "LemusStudio")
    except Exception:
        pass
    return None

def get_storage_root():
    global _STORAGE_ROOT
    if _STORAGE_ROOT:
        return _STORAGE_ROOT
    candidates = []
    if platform == "android":
        candidates.append("/storage/emulated/0/Music/LemusStudio")
        p = _android_private()
        if p:
            candidates.append(p)
    else:
        candidates.append(os.path.join(os.path.expanduser("~"), "LemusStudio"))
    for p in candidates:
        try:
            os.makedirs(p, exist_ok=True)
            t = os.path.join(p, ".test")
            with open(t, "w") as f: f.write("1")
            os.remove(t)
            _STORAGE_ROOT = p
            return p
        except Exception:
            continue
    _STORAGE_ROOT = "."
    return _STORAGE_ROOT

# =====================================================================
# МУЗЫКАЛЬНАЯ ТЕОРИЯ И ПРОЦЕДУРНЫЙ ДВИЖОК V4 (FULL RESTORE)
# =====================================================================
NOTE_SEMI = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5,
             "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
SCALE_MINOR = [0, 2, 3, 5, 7, 8, 10]
SCALE_MAJOR = [0, 2, 4, 5, 7, 9, 11]
PROG_MINOR = [0, 5, 2, 6]   # i - VI - III - VII
PROG_MAJOR = [0, 5, 3, 4]   # I - vi - IV - V

def _parse_key(key_str):
    s = (key_str or "").strip()
    m = re.match(r"([A-Ga-g])([#b]?)", s)
    if not m:
        return 220.0, True
    semi = NOTE_SEMI.get(m.group(1).upper() + m.group(2), 9)
    minor = ("minor" in s.lower()) or ("минор" in s.lower()) or (s.endswith("m") and "maj" not in s.lower())
    hz = 220.0 * (2 ** ((semi - 9) / 12.0))
    return hz, minor

def _chord_freqs(root_hz, scale, degree):
    out = []
    for step in (0, 2, 4, 6):
        idx = (degree + step) % 7
        octv = (degree + step) // 7
        semi = scale[idx] + 12 * octv
        out.append(root_hz * (2 ** (semi / 12.0)))
    return out

def _section_energy(name):
    n = (name or "").lower()
    if "intro" in n or "вступ" in n: return 0.30
    if "chorus" in n or "припев" in n or "drop" in n: return 0.95
    if "bridge" in n or "бридж" in n or "break" in n: return 0.45
    if "outro" in n or "финал" in n or "концов" in n: return 0.35
    return 0.60

def _degree_to_freq(root_hz, scale, degree, octave_shift=0):
    idx = degree % 7
    octv = degree // 7 + octave_shift
    semi = scale[idx] + 12 * octv
    return root_hz * (2 ** (semi / 12.0))

class ProceduralAudioEngineV4:
    """
    V4 — механика переписана с нуля, чтобы уйти от «зацикленного мотивчика».
    Что изменилось по сравнению с V3:
     1. МЕЛОДИЯ больше не берётся из одного статического 8-нотного мотива,
        который крутился по кругу на весь трек. Теперь на каждую СЕКЦИЮ
        (куплет/припев/бридж) генерируется своя мелодическая фраза —
        пошаговое движение по диатонической гамме с редкими скачками,
        паузами и синкопами, плюс её начало «наследует» последнюю ноту
        предыдущей фразы (плавное голосоведение, а не рандомный обрыв).
     2. АРАНЖИРОВКА отличается по слоям для куплета/припева/бриджа, а не
        просто по громкости одних и тех же слоёв.
     3. САЙДЧЕЙН-ДАКИНГ: гармонические слои (пэд/бас/лид) подсаживаются
        на ~120 мс под каждый удар кика — характерный «дышащий» приём
        современного продакшена, которого в V3 не было вовсе.
     4. РЕВЕРБ/ДИЛЕЙ-СЕНД: короткий стерео slap-delay на общей шине —
        без него любой синтезатор звучит сухо и дёшево.
     5. Более чистый вокинг пэда (не все 4 тона хора в одном регистре
        поверх баса — это и давало «кашу»), лёгкая хумантзация тайминга/
        громкости ударных.
    """
    def __init__(self, sr=22050):
        self.sr = sr

    def _make_phrase(self, rnd, beats_total, energy, start_degree):
        events = []
        degree = start_degree
        t = 0.0
        if energy > 0.8:
            durs = [0.5, 0.5, 0.5, 1.0, 0.25, 0.25]
            rest_p = 0.06
            leap_p = 0.30
        elif energy > 0.5:
            durs = [1.0, 1.0, 0.5, 1.5, 0.5]
            rest_p = 0.14
            leap_p = 0.18
        else:
            durs = [1.5, 2.0, 1.0, 2.0]
            rest_p = 0.22
            leap_p = 0.10
        
        steps_since_rest = 0
        while t < beats_total - 0.01:
            dur = rnd.choice(durs)
            if t + dur > beats_total:
                dur = beats_total - t
            
            if rnd.random() < rest_p and steps_since_rest > 1:
                events.append((t, dur, None))
                steps_since_rest = 0
            else:
                if rnd.random() < leap_p:
                    move = rnd.choice([-4, -3, 3, 4, 5])
                else:
                    move = rnd.choice([-2, -1, -1, 0, 1, 1, 2])
                degree = max(-3, min(10, degree + move))
                events.append((t, dur, degree))
                steps_since_rest += 1
            t += dur
            
        if events and events[-1][2] is not None:
            target = rnd.choice([0, 2])
            off, dur, _ = events[-1]
            events[-1] = (off, dur, target)
            degree = target
        return events, degree

    def generate(self, duration, seed_text, plan=None):
        import random
        plan = plan or {}
        rnd = random.Random(int(hashlib.md5((seed_text or "lemus").encode()).hexdigest()[:8], 16))
        dur = max(20, min(int(duration or 60), 60))
        n = int(self.sr * dur)
        buf_l = [0.0] * n
        buf_r = [0.0] * n
        
        bpm = int(plan.get("bpm") or 0) or rnd.choice([96, 104, 112, 122, 128])
        bpm = max(60, min(190, bpm))
        root_hz, minor = _parse_key(plan.get("key"))
        scale = SCALE_MINOR if minor else SCALE_MAJOR
        prog = PROG_MINOR if minor else PROG_MAJOR
        chords = [_chord_freqs(root_hz, scale, d) for d in prog]
        
        beat_samples = int(self.sr * 60.0 / bpm)
        bar_samples = beat_samples * 4
        bars_total = n // bar_samples + 1
        
        sections = plan.get("sections") or []
        segments = []
        if sections:
            for sec in sections:
                bars = max(1, int(sec.get("bars") or 4))
                e = sec.get("energy")
                e = float(e) if e is not None else _section_energy(sec.get("name"))
                segments.append((sec.get("name", ""), bars, e))
        
        if not segments:
            segments = [("Intro", 2, 0.28), ("Verse", 4, 0.55), ("Chorus", 4, 0.95),
                        ("Verse", 4, 0.58), ("Chorus", 4, 0.95), ("Bridge", 2, 0.45),
                        ("Chorus", 4, 0.95), ("Outro", 2, 0.25)]
        
        covered = sum(s[1] for s in segments)
        while covered < bars_total:
            segments = segments + segments
            covered = sum(s[1] for s in segments)
            
        kick_times = []
        melody_degree = 0
        bar_cursor = 0
        chord_i = 0
        
        while bar_cursor < bars_total and segments:
            name, seg_bars, energy = segments.pop(0)
            seg_bars = min(seg_bars, bars_total - bar_cursor)
            if seg_bars <= 0: continue
            
            is_chorus = "chorus" in name.lower() or "припев" in name.lower() or "drop" in name.lower()
            is_verse = "verse" in name.lower() or "куплет" in name.lower()
            
            phrase, melody_degree = self._make_phrase(rnd, seg_bars * 4, energy, melody_degree)
            
            for b in range(seg_bars):
                bar_idx = bar_cursor + b
                ch_freqs = chords[chord_i % 4]
                if b % 2 == 1: chord_i += 1
                start_s = bar_idx * bar_samples
                
                pad_amp = 0.055 + 0.045 * energy
                for freq in ch_freqs[1:]:
                    self._add_pad(buf_l, buf_r, start_s, bar_samples, freq, pad_amp, detune=0.0035)
                
                bass_f = ch_freqs[0] / 2.0
                bass_amp = 0.15 + 0.12 * energy
                pattern = [0.0, 1.0, 2.0, 3.0] if energy > 0.8 else [0.0, 2.0]
                for beat_off in pattern:
                    bs = start_s + int(beat_off * beat_samples)
                    bl = min(int(beat_samples * 0.9), n - bs)
                    if bl > 0:
                        f = bass_f * (1.5 if (energy > 0.85 and rnd.random() < 0.18) else 1.0)
                        self._add_bass(buf_l, buf_r, bs, bl, f, bass_amp)
                
                if energy > 0.35:
                    kick_steps = [0.0, 2.0] if energy <= 0.8 else [0.0, 0.75, 2.0, 2.75]
                    for beat_off in kick_steps:
                        jitter = rnd.uniform(-0.006, 0.006) * beat_samples
                        ks = start_s + int(beat_off * beat_samples + jitter)
                        kl = min(int(0.15 * self.sr), n - ks)
                        if kl > 0 and 0 <= ks < n:
                            amp = (0.55 + 0.15 * energy) * rnd.uniform(0.92, 1.0)
                            self._add_kick(buf_l, buf_r, ks, kl, amp)
                            kick_times.append(ks)
                    
                    if energy > 0.55:
                        for beat_off in (1.0, 3.0):
                            ss = start_s + int(beat_off * beat_samples)
                            sl = min(int(0.14 * self.sr), n - ss)
                            if sl > 0:
                                self._add_snare(buf_l, buf_r, ss, sl, (0.28 + 0.1 * energy) * rnd.uniform(0.9, 1.0))
                    
                    hh_count = 8 if energy > 0.75 else 4
                    hh_step = bar_samples // hh_count
                    for k in range(hh_count):
                        hs = start_s + k * hh_step + rnd.randint(-40, 40)
                        hl = min(int(0.045 * self.sr), n - hs)
                        if hl > 0 and 0 <= hs < n:
                            amp = 0.10 * energy * rnd.uniform(0.7, 1.0)
                            if k % 2 == 0:
                                self._add_hat(buf_l, buf_r, hs, hl, amp * 0.8, amp * 1.2)
                            else:
                                self._add_hat(buf_l, buf_r, hs, hl, amp * 1.2, amp * 0.8)
                
                bar_start_beat = b * 4.0
                bar_end_beat = bar_start_beat + 4.0
                for off, ndur, degree in phrase:
                    if degree is None or not (bar_start_beat <= off < bar_end_beat): continue
                    if not is_chorus and not is_verse and energy < 0.35: continue
                    
                    octave_shift = 1 if is_chorus else 0
                    freq = _degree_to_freq(root_hz, scale, degree, octave_shift=octave_shift)
                    ms = start_s + int((off - bar_start_beat) * beat_samples)
                    ml = int(ndur * beat_samples * 0.92)
                    if ml <= 0 or ms >= n: continue
                    
                    lead_amp = (0.13 if is_chorus else 0.08) * (0.6 + 0.4 * energy)
                    pan = -0.2 if int(off * 2) % 2 == 0 else 0.2
                    self._add_lead(buf_l, buf_r, ms, ml, freq, lead_amp, vib=rnd.uniform(0.8, 1.15), pan=pan)
            
            bar_cursor += seg_bars

        self._apply_sidechain(buf_l, buf_r, kick_times, n)
        self._apply_slap_delay(buf_l, buf_r, n)
        return self._master_encode(buf_l, buf_r, n)

    def _apply_sidechain(self, L, R, kick_times, n):
        if not kick_times: return
        dip_len = int(0.015 * self.sr)
        rel_len = int(0.13 * self.sr)
        window = dip_len + rel_len
        for kt in kick_times:
            end = min(n, kt + window)
            for i in range(kt, end):
                rel = i - kt
                if rel < dip_len:
                    g = 1.0 - 0.55 * (rel / max(1, dip_len))
                else:
                    g = 0.45 + 0.55 * ((rel - dip_len) / max(1, rel_len))
                L[i] *= g
                R[i] *= g

    def _apply_slap_delay(self, L, R, n):
        delay = int(0.27 * self.sr)
        fb = 0.24
        if delay >= n: return
        for i in range(delay, n):
            L[i] += R[i - delay] * fb
            R[i] += L[i - delay] * fb

    def _adsr(self, i, length, attack, decay, sustain, release):
        if i >= length: return 0.0
        pos = i / length
        if pos < attack: return pos / attack
        elif pos < attack + decay: return 1.0 - (pos - attack)/decay * (1.0 - sustain)
        elif pos < 1.0 - release: return sustain
        else: return sustain * (1.0 - (pos - (1.0 - release))/release)

    def _add_pad(self, L, R, start, length, freq, amp, detune=0.0):
        end = min(start + length, len(L))
        ln = end - start
        if ln <= 0: return
        w1 = 2 * math.pi * freq / self.sr
        w2 = 2 * math.pi * (freq * (1 + detune)) / self.sr
        att, dec, sus, rel = 0.1, 0.2, 0.7, 0.15
        prev = 0.0
        for i in range(ln):
            env = self._adsr(i, ln, att, dec, sus, rel)
            v = math.sin(w1 * i) + math.sin(w2 * i)
            prev = prev + 0.5 * (v - prev)
            val = amp * env * prev * 0.5
            L[start + i] += val
            R[start + i] += val

    def _add_bass(self, L, R, start, length, freq, amp):
        end = min(start + length, len(L))
        ln = end - start
        if ln <= 0: return
        w = 2 * math.pi * freq / self.sr
        att, dec, sus, rel = 0.01, 0.3, 0.5, 0.1
        prev_val = 0.0
        lp_coeff = 0.12
        for i in range(ln):
            env = self._adsr(i, ln, att, dec, sus, rel)
            phase = (i * freq / self.sr) % 1.0
            raw = 2.0 * phase - 1.0
            raw += 0.5 * math.sin(w * i)
            filtered = prev_val + lp_coeff * (raw - prev_val)
            prev_val = filtered
            val = amp * env * filtered
            L[start + i] += val
            R[start + i] += val

    def _add_kick(self, L, R, start, length, amp):
        end = min(start + length, len(L))
        ln = end - start
        if ln <= 0: return
        for i in range(ln):
            t = i / self.sr
            f = 120 * math.exp(-t * 15) + 40
            env = math.exp(-t * 10)
            val = amp * env * math.sin(2 * math.pi * f * t)
            L[start + i] += val
            R[start + i] += val

    def _add_snare(self, L, R, start, length, amp):
        end = min(start + length, len(L))
        ln = end - start
        if ln <= 0: return
        for i in range(ln):
            t = i / self.sr
            noise = random.uniform(-1, 1)
            tone = math.sin(2 * math.pi * 180 * t)
            env = math.exp(-t * 20)
            val = amp * env * (0.7 * noise + 0.3 * tone)
            L[start + i] += val
            R[start + i] += val

    def _add_hat(self, L, R, start, length, amp_l, amp_r):
        end = min(start + length, len(L))
        ln = end - start
        if ln <= 0: return
        for i in range(ln):
            t = i / self.sr
            noise = random.uniform(-1, 1)
            env = math.exp(-t * 50)
            v = noise * env
            L[start + i] += v * amp_l
            R[start + i] += v * amp_r

    def _add_lead(self, L, R, start, length, freq, amp, vib=1.0, pan=0.0):
        end = min(start + length, len(L))
        ln = end - start
        if ln <= 0: return
        w = 2 * math.pi * freq / self.sr
        att, dec, sus, rel = 0.02, 0.2, 0.4, 0.12
        lg = 0.5 * (1 - pan)
        rg = 0.5 * (1 + pan)
        for i in range(ln):
            t = i / self.sr
            env = self._adsr(i, ln, att, dec, sus, rel)
            fm_mod = 1.0 + 0.01 * vib * math.sin(2 * math.pi * 5.5 * t)
            carrier = math.sin(w * fm_mod * i)
            harmonics = 0.5 * math.sin(2 * w * fm_mod * i)
            val = amp * env * (carrier + harmonics)
            L[start + i] += val * lg * 2
            R[start + i] += val * rg * 2

    def _master_encode(self, L, R, n):
        peak = 0.0
        for i in range(n):
            if abs(L[i]) > peak: peak = abs(L[i])
            if abs(R[i]) > peak: peak = abs(R[i])
        gain = 0.95 / peak if peak > 0 else 1.0
        
        data = bytearray()
        fade_in = int(0.5 * self.sr)
        fade_out = int(1.5 * self.sr)
        
        for i in range(n):
            vl = L[i] * gain
            vr = R[i] * gain
            if i < fade_in:
                f = i / fade_in
                vl *= f; vr *= f
            elif i > n - fade_out:
                f = (n - i) / fade_out
                vl *= f; vr *= f
            
            vl = math.tanh(vl * 1.2) * 0.85
            vr = math.tanh(vr * 1.2) * 0.85
            
            il = int(max(-32768, min(32767, vl * 32767)))
            ir = int(max(-32768, min(32767, vr * 32767)))
            data.extend(struct.pack('<hh', il, ir))
            
        header = b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' + \
                 struct.pack('<IHHIIHH', 16, 1, 2, self.sr, self.sr*4, 4, 16) + \
                 b'data' + struct.pack('<I', len(data))
        return header + bytes(data)

def _looks_like_audio(d):
    if not d or len(d) < 2000: return False
    return (d[:3] == b"ID3" or d[:2] in (b"\xff\xfb", b"\xff\xf3") or d[:4] in (b"RIFF", b"OggS", b"fLaC"))

class SeekLine(Widget):
    value = NumericProperty(0.0)
    _drag = False
    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self._drag = True
            self._apply(touch)
            return True
        return super().on_touch_down(touch)
    def on_touch_move(self, touch):
        if self._drag:
            self._apply(touch)
            return True
        return super().on_touch_move(touch)
    def on_touch_up(self, touch):
        if self._drag:
            self._drag = False
            self._apply(touch)
            app = MDApp.get_running_app()
            if app: app.seek_ratio(self.value)
            return True
        return super().on_touch_up(touch)
    def _apply(self, touch):
        ratio = (touch.x - self.x) / max(1.0, self.width)
        self.value = max(0.0, min(1.0, ratio))

Factory.register('SeekLine', cls=SeekLine)

# ==============================================================================
# KV LANGUAGE (FULL RESTORE FROM V7.3.1)
# ==============================================================================
KV = '''
<SeekLine>:
    canvas:
        Color:
            rgba: 0.26, 0.24, 0.36, 1
        Rectangle:
            pos: self.pos
            size: self.size
        Color:
            rgba: 1.0, 0.85, 0.35, 1
        Rectangle:
            pos: self.pos
            size: self.width * self.value, self.height

MDBoxLayout:
    orientation: "vertical"
    canvas.before:
        Color:
            rgba: 0.066, 0.064, 0.10, 1
        Rectangle:
            pos: self.pos
            size: self.size

    MDTopAppBar:
        title: "Lemus AI Music Studio"
        elevation: 0
        md_bg_color: 0.066, 0.064, 0.10, 1
        specific_text_color: 0.97, 0.96, 1, 1
        right_action_items: [["shield-key-outline", lambda x: app.show_vault_status()], ["information-outline", lambda x: app.show_onboarding()]]

    MDBoxLayout:
        id: player_bar
        orientation: "vertical"
        size_hint_y: None
        height: "0dp"
        md_bg_color: 0.101, 0.098, 0.156, 1
        opacity: 0 if self.height == 0 else 1
        SeekLine:
            id: player_seek
            size_hint_y: None
            height: "4dp"
        MDBoxLayout:
            size_hint_y: None
            height: "60dp"
            padding: "8dp"
            spacing: "6dp"
            Image:
                id: player_cover
                size_hint: None, None
                size: "44dp", "44dp"
                allow_stretch: True
                keep_ratio: True
                opacity: 0
            MDBoxLayout:
                orientation: "vertical"
                MDLabel:
                    id: player_title
                    text: ""
                    shorten: True
                    theme_text_color: "Custom"
                    text_color: 0.97, 0.96, 1, 1
                    font_style: "Subtitle2"
                MDLabel:
                    id: player_artist
                    text: ""
                    shorten: True
                    theme_text_color: "Custom"
                    text_color: 0.60, 0.58, 0.70, 1
                    font_style: "Caption"
            MDIconButton:
                icon: "skip-previous"
                theme_text_color: "Custom"
                text_color: 0.97, 0.96, 1, 1
                on_release: app.player_prev()
            MDIconButton:
                id: player_play_btn
                icon: "pause"
                theme_text_color: "Custom"
                text_color: 1.0, 0.85, 0.35, 1
                on_release: app.player_toggle()
            MDIconButton:
                icon: "skip-next"
                theme_text_color: "Custom"
                text_color: 0.97, 0.96, 1, 1
                on_release: app.player_next()
            MDIconButton:
                icon: "download"
                theme_text_color: "Custom"
                text_color: 0.6, 0.85, 0.7, 1
                on_release: app.download_current()
            MDLabel:
                id: player_pos
                text: "0:00"
                size_hint_x: None
                width: "42dp"
                halign: "center"
                theme_text_color: "Custom"
                text_color: 0.60, 0.58, 0.70, 1

    MDBottomNavigation:
        id: bottom_nav
        panel_color: 0.101, 0.098, 0.156, 1
        text_color_active: 1.0, 0.85, 0.35, 1
        text_color_normal: 0.50, 0.48, 0.58, 1

        MDBottomNavigationItem:
            name: "tab_studio"
            text: "Студия"
            icon: "waveform"
            MDBoxLayout:
                orientation: "vertical"
                canvas.before:
                    Color:
                        rgba: 0.066, 0.064, 0.10, 1
                    Rectangle:
                        pos: self.pos
                        size: self.size
                MDBoxLayout:
                    size_hint_y: None
                    height: "52dp"
                    padding: "10dp"
                    spacing: "8dp"
                    ScrollView:
                        do_scroll_y: False
                        MDBoxLayout:
                            size_hint_x: None
                            width: self.minimum_width
                            spacing: "8dp"
                            MDRaisedButton:
                                id: chip_single
                                text: "Сингл"
                                elevation: 0
                                md_bg_color: 0.42, 0.34, 0.78, 1
                                on_release: app.switch_mode("single")
                            MDRaisedButton:
                                id: chip_prompt
                                text: "Промпт"
                                elevation: 0
                                md_bg_color: 0.145, 0.138, 0.196, 1
                                on_release: app.switch_mode("prompt")
                            MDRaisedButton:
                                id: chip_viral
                                text: "Хит"
                                elevation: 0
                                md_bg_color: 0.145, 0.138, 0.196, 1
                                on_release: app.switch_mode("viral")
                            MDRaisedButton:
                                id: chip_album
                                text: "Альбом"
                                elevation: 0
                                md_bg_color: 0.145, 0.138, 0.196, 1
                                on_release: app.switch_mode("album")
                            MDRaisedButton:
                                id: chip_money
                                text: "Фон"
                                elevation: 0
                                md_bg_color: 0.145, 0.138, 0.196, 1
                                on_release: app.switch_mode("money")
                            MDRaisedButton:
                                id: chip_var
                                text: "Вариация 2"
                                elevation: 0
                                md_bg_color: 0.48, 0.32, 0.22, 1
                                on_release: app.make_variation()
                BoxLayout:
                    id: studio_area

        MDBottomNavigationItem:
            name: "tab_projects"
            text: "Медиатека"
            icon: "folder-music"
            MDBoxLayout:
                orientation: "vertical"
                padding: "12dp"
                spacing: "8dp"
                MDTextField:
                    id: search_input
                    hint_text: "Поиск по названию / жанру"
                    mode: "rectangle"
                    size_hint_y: None
                    height: "48dp"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 1.0, 0.85, 0.35, 1
                    on_text: app.filter_projects(self.text)
                ScrollView:
                    size_hint_y: None
                    height: "40dp"
                    do_scroll_y: False
                    MDBoxLayout:
                        size_hint_x: None
                        width: self.minimum_width
                        spacing: "2dp"
                        MDFlatButton:
                            text: "Все"
                            theme_text_color: "Custom"
                            text_color: 0.80, 0.75, 0.95, 1
                            on_release: app.filter_by_type("all")
                        MDFlatButton:
                            text: "Сингл"
                            theme_text_color: "Custom"
                            text_color: 0.80, 0.75, 0.95, 1
                            on_release: app.filter_by_type("Single")
                        MDFlatButton:
                            text: "Хит"
                            theme_text_color: "Custom"
                            text_color: 0.80, 0.75, 0.95, 1
                            on_release: app.filter_by_type("Viral")
                        MDFlatButton:
                            text: "Альбом"
                            theme_text_color: "Custom"
                            text_color: 0.80, 0.75, 0.95, 1
                            on_release: app.filter_by_type("EP")
                        MDFlatButton:
                            text: "Фон"
                            theme_text_color: "Custom"
                            text_color: 0.80, 0.75, 0.95, 1
                            on_release: app.filter_by_type("Money")
                        MDFlatButton:
                            text: "Промпт"
                            theme_text_color: "Custom"
                            text_color: 0.80, 0.75, 0.95, 1
                            on_release: app.filter_by_type("Prompt")
                MDBoxLayout:
                    size_hint_y: None
                    height: "44dp"
                    spacing: "8dp"
                    MDRaisedButton:
                        text: "Обновить"
                        size_hint_x: 1
                        elevation: 0
                        md_bg_color: 0.145, 0.138, 0.196, 1
                        on_release: app.refresh_projects_ui()
                    MDRaisedButton:
                        text: "Выбрать"
                        size_hint_x: 1
                        elevation: 0
                        md_bg_color: 0.55, 0.40, 0.22, 1
                        on_release: app.toggle_selection_mode()
                MDBoxLayout:
                    size_hint_y: None
                    height: "44dp"
                    spacing: "8dp"
                    MDRaisedButton:
                        text: "Калькулятор"
                        size_hint_x: 1
                        elevation: 0
                        md_bg_color: 0.24, 0.50, 0.46, 1
                        on_release: app.show_revenue_calculator()
                    MDRaisedButton:
                        text: "Гайд"
                        size_hint_x: 1
                        elevation: 0
                        md_bg_color: 0.24, 0.42, 0.70, 1
                        on_release: app.show_monetize_guide()
                MDBoxLayout:
                    id: selection_bar
                    size_hint_y: None
                    height: "0dp"
                    spacing: "8dp"
                    md_bg_color: 0.30, 0.12, 0.12, 1
                    opacity: 0 if self.height == 0 else 1
                    MDLabel:
                        id: selection_count
                        text: "Выбрано: 0"
                        theme_text_color: "Custom"
                        text_color: 1, 0.85, 0.85, 1
                    MDRaisedButton:
                        text: "Удалить"
                        elevation: 0
                        md_bg_color: 0.68, 0.24, 0.24, 1
                        on_release: app.delete_selected()
                    MDFlatButton:
                        text: "Отмена"
                        theme_text_color: "Custom"
                        text_color: 0.85, 0.85, 0.85, 1
                        on_release: app.toggle_selection_mode()
                MDScrollView:
                    MDList:
                        id: projects_list_container

        MDBottomNavigationItem:
            name: "tab_settings"
            text: "Настройки"
            icon: "cog"
            MDScrollView:
                MDBoxLayout:
                    orientation: "vertical"
                    padding: "16dp"
                    spacing: "14dp"
                    size_hint_y: None
                    height: self.minimum_height
                    MDCard:
                        orientation: "vertical"
                        padding: "16dp"
                        radius: [24, 24, 24, 24]
                        elevation: 0
                        size_hint_y: None
                        height: "320dp"
                        md_bg_color: 0.101, 0.098, 0.156, 1
                        MDBoxLayout:
                            orientation: "vertical"
                            spacing: "14dp"
                            MDLabel:
                                text: "Активация LemusAI"
                                font_style: "H6"
                                theme_text_color: "Custom"
                                text_color: 0.97, 0.96, 1, 1
                            MDRaisedButton:
                                text: "Загрузить keys.json"
                                size_hint_x: 1
                                elevation: 0
                                md_bg_color: 0.24, 0.42, 0.70, 1
                                on_release: app.open_file_manager("keys")
                            MDLabel:
                                text: "— или —"
                                halign: "center"
                                theme_text_color: "Custom"
                                text_color: 0.60, 0.58, 0.70, 1
                            MDTextField:
                                id: master_password_input
                                hint_text: "Пароль LemusAI (gist)"
                                password: True
                                mode: "rectangle"
                                line_color_normal: 0.26, 0.24, 0.36, 1
                                line_color_focus: 0.78, 0.46, 0.20, 1
                            MDRaisedButton:
                                text: "Активировать через gist"
                                size_hint_x: 1
                                elevation: 0
                                md_bg_color: 0.78, 0.46, 0.20, 1
                                on_release: app.unlock_master_keys()
                            MDLabel:
                                id: activation_status
                                text: "Не активировано"
                                theme_text_color: "Custom"
                                text_color: 0.60, 0.58, 0.70, 1
                                halign: "center"
                    MDCard:
                        orientation: "vertical"
                        padding: "16dp"
                        radius: [24, 24, 24, 24]
                        elevation: 0
                        size_hint_y: None
                        height: "170dp"
                        md_bg_color: 0.101, 0.098, 0.156, 1
                        MDBoxLayout:
                            orientation: "vertical"
                            spacing: "12dp"
                            MDBoxLayout:
                                size_hint_y: None
                                height: "44dp"
                                spacing: "8dp"
                                MDLabel:
                                    text: "Указывать AI в релизах"
                                    theme_text_color: "Custom"
                                    text_color: 0.84, 0.82, 0.94, 1
                                MDSwitch:
                                    id: ai_disclose_switch
                                    active: True
                                    on_active: app.set_disclose_ai(self.active)
                            MDBoxLayout:
                                spacing: "8dp"
                                size_hint_y: None
                                height: "46dp"
                                MDRaisedButton:
                                    text: "Диагностика"
                                    size_hint_x: 1
                                    elevation: 0
                                    md_bg_color: 0.50, 0.32, 0.60, 1
                                    on_release: app.run_key_diagnostics()
                                MDRaisedButton:
                                    text: "Резерв Yandex"
                                    size_hint_x: 1
                                    elevation: 0
                                    md_bg_color: 0.68, 0.24, 0.24, 1
                                    on_release: app.backup_to_yandex()
                            MDRaisedButton:
                                text: "Проверить обновления"
                                elevation: 0
                                md_bg_color: 0.28, 0.38, 0.60, 1
                                on_release: app.check_for_updates()
                    MDCard:
                        orientation: "vertical"
                        padding: "16dp"
                        radius: [24, 24, 24, 24]
                        elevation: 0
                        size_hint_y: None
                        height: "130dp"
                        md_bg_color: 0.101, 0.098, 0.156, 1
                        MDBoxLayout:
                            orientation: "vertical"
                            spacing: "12dp"
                            MDRaisedButton:
                                text: "Показать логи"
                                elevation: 0
                                md_bg_color: 0.48, 0.32, 0.22, 1
                                on_release: app.show_crash_log()
                            MDRaisedButton:
                                text: "Отправить логи"
                                elevation: 0
                                md_bg_color: 0.24, 0.42, 0.70, 1
                                on_release: app.send_logs_to_me()
                    MDLabel:
                        text: "Настоящий ИИ-синтез (Replicate) — студийное качество"
                        font_style: "Subtitle1"
                        theme_text_color: "Custom"
                        text_color: 0.84, 0.82, 0.94, 1
                    MDTextField:
                        id: cfg_replicate
                        hint_text: "Replicate API Token (r8_...) — реальная генерация музыки"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDLabel:
                        text: "Свои API-ключи (гостевой режим)"
                        font_style: "Subtitle1"
                        theme_text_color: "Custom"
                        text_color: 0.84, 0.82, 0.94, 1
                    MDTextField:
                        id: cfg_gemini
                        hint_text: "Google Gemini (AIza... из aistudio.google.com/apikey)"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDTextField:
                        id: cfg_openrouter
                        hint_text: "OpenRouter Key"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDTextField:
                        id: cfg_yandex
                        hint_text: "Yandex Disk Token"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDTextField:
                        id: cfg_hf
                        hint_text: "Hugging Face Token"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDTextField:
                        id: cfg_fish
                        hint_text: "Fish.audio Token"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDTextField:
                        id: cfg_groq
                        hint_text: "Groq Whisper Token"
                        mode: "rectangle"
                        line_color_normal: 0.26, 0.24, 0.36, 1
                        line_color_focus: 0.60, 0.48, 0.96, 1
                    MDRaisedButton:
                        text: "Сохранить свои ключи"
                        size_hint_x: 1
                        elevation: 0
                        md_bg_color: 0.20, 0.38, 0.70, 1
                        on_release: app.save_user_settings()
                    MDRaisedButton:
                        text: "Сбросить все ключи"
                        size_hint_x: 1
                        elevation: 0
                        md_bg_color: 0.68, 0.24, 0.24, 1
                        on_release: app.reset_all_keys()
                    MDLabel:
                        id: version_label
                        text: ""
                        theme_text_color: "Custom"
                        text_color: 0.50, 0.48, 0.58, 1
                        font_style: "Caption"
                        halign: "center"
'''

FORM_SINGLE = '''
MDScrollView:
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "14dp"
        size_hint_y: None
        height: self.minimum_height
        MDCard:
            orientation: "vertical"
            padding: "14dp"
            radius: [18, 18, 18, 18]
            elevation: 6
            size_hint_y: None
            height: "92dp"
            md_bg_color: 0.16, 0.13, 0.28, 1
            MDLabel:
                text: "LEMUS"
                font_style: "H4"
                bold: True
                theme_text_color: "Custom"
                text_color: 0.82, 0.68, 1, 1
                halign: "center"
            MDLabel:
                text: "СТУДИЯ МУЗЫКИ С ИИ"
                font_style: "Caption"
                theme_text_color: "Custom"
                text_color: 0.62, 0.58, 0.78, 1
                halign: "center"
        MDCard:
            orientation: "vertical"
            padding: "16dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "310dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDBoxLayout:
                orientation: "vertical"
                spacing: "14dp"
                MDTextField:
                    id: s_title_input
                    hint_text: "Тема / идея трека"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.5, 0.95, 1
                MDTextField:
                    id: s_genre_input
                    hint_text: "Жанр (любой гибрид)"
                    text: "Pop"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.5, 0.95, 1
                MDTextField:
                    id: s_duration_input
                    hint_text: "Длительность (сек)"
                    text: "90"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.5, 0.95, 1
                MDBoxLayout:
                    spacing: "8dp"
                    size_hint_y: None
                    height: "46dp"
                    MDRaisedButton:
                        text: "Демоголос"
                        elevation: 0
                        md_bg_color: 0.30, 0.26, 0.48, 1
                        on_release: app.open_file_manager("voice")
                    MDIconButton:
                        icon: "close-circle-outline"
                        theme_text_color: "Custom"
                        text_color: 0.60, 0.56, 0.76, 1
                        on_release: app.clear_voice_sample()
                MDLabel:
                    id: voice_sample_label
                    text: "Голос: не выбран"
                    theme_text_color: "Custom"
                    text_color: 0.60, 0.58, 0.70, 1
                    font_style: "Caption"
        MDCard:
            orientation: "vertical"
            padding: "12dp"
            radius: [18, 18, 18, 18]
            elevation: 0
            size_hint_y: None
            height: "90dp"
            md_bg_color: 0.078, 0.076, 0.118, 1
            MDLabel:
                id: s_enhanced_label
                text: "Улучшенный промпт появится здесь"
                theme_text_color: "Custom"
                text_color: 0.60, 0.58, 0.70, 1
                font_style: "Caption"
        MDBoxLayout:
            spacing: "8dp"
            size_hint_y: None
            height: "46dp"
            MDRaisedButton:
                text: "Улучшить промпт"
                size_hint_x: 1
                elevation: 0
                md_bg_color: 0.20, 0.56, 0.44, 1
                on_release: app.enhance_single()
            MDRaisedButton:
                text: "История"
                size_hint_x: 1
                elevation: 0
                md_bg_color: 0.145, 0.138, 0.196, 1
                on_release: app.show_prompt_history()
        MDRaisedButton:
            text: "Сгенерировать сингл"
            size_hint_x: 1
            elevation: 0
            md_bg_color: 0.42, 0.34, 0.78, 1
            on_release: app.start_single_generation()
        MDCard:
            orientation: "vertical"
            padding: "14dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "130dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDLabel:
                id: s_status_label
                text: "Студия готова"
                theme_text_color: "Custom"
                text_color: 0.74, 0.72, 0.84, 1
            MDProgressBar:
                id: s_progress
                value: 0
                max: 100
'''

FORM_PROMPT = '''
MDScrollView:
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "14dp"
        size_hint_y: None
        height: self.minimum_height
        MDCard:
            orientation: "vertical"
            padding: "16dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "300dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDBoxLayout:
                orientation: "vertical"
                spacing: "14dp"
                MDLabel:
                    text: "Черновик идеи (можно коротко и с опечатками)"
                    theme_text_color: "Custom"
                    text_color: 0.74, 0.72, 0.84, 1
                MDTextField:
                    id: p_prompt_input
                    hint_text: "Например: песня Олеси про работу бухгалтером, мягкий drum and bass с женским вокалом"
                    mode: "rectangle"
                    multiline: True
                    size_hint_y: None
                    height: "150dp"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.4, 0.75, 0.6, 1
                MDTextField:
                    id: p_duration_input
                    hint_text: "Длительность (сек)"
                    text: "120"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.4, 0.75, 0.6, 1
        MDCard:
            orientation: "vertical"
            padding: "12dp"
            radius: [18, 18, 18, 18]
            elevation: 0
            size_hint_y: None
            height: "110dp"
            md_bg_color: 0.078, 0.076, 0.118, 1
            MDLabel:
                id: p_enhanced_label
                text: "Улучшенный промпт появится здесь"
                theme_text_color: "Custom"
                text_color: 0.60, 0.58, 0.70, 1
                font_style: "Caption"
        MDRaisedButton:
            text: "Улучшить промпт"
            size_hint_x: 1
            elevation: 0
            md_bg_color: 0.20, 0.56, 0.44, 1
            on_release: app.enhance_prompt_mode()
        MDRaisedButton:
            text: "Создать трек по промпту"
            size_hint_x: 1
            elevation: 0
            md_bg_color: 0.42, 0.34, 0.78, 1
            on_release: app.start_prompt_generation()
        MDCard:
            orientation: "vertical"
            padding: "14dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "130dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDLabel:
                id: p_status_label
                text: "Опиши идею и нажми кнопку"
                theme_text_color: "Custom"
                text_color: 0.74, 0.72, 0.84, 1
            MDProgressBar:
                id: p_progress
                value: 0
                max: 100
        MDSeparator:
            height: "2dp"
        MDLabel:
            text: "Фоновый трек для стримингов (доход)"
            font_style: "Subtitle1"
            theme_text_color: "Custom"
            text_color: 0.84, 0.82, 0.94, 1
        MDTextField:
            id: m_niche_input
            hint_text: "Ниша"
            text: "Lo-Fi Study Beats"
            mode: "rectangle"
            line_color_normal: 0.26, 0.24, 0.36, 1
            line_color_focus: 0.4, 0.75, 0.6, 1
        MDTextField:
            id: m_duration_input
            hint_text: "Хронометраж (сек)"
            text: "150"
            mode: "rectangle"
            line_color_normal: 0.26, 0.24, 0.36, 1
            line_color_focus: 0.4, 0.75, 0.6, 1
        MDRaisedButton:
            text: "Создать фоновый трек"
            size_hint_x: 1
            elevation: 0
            md_bg_color: 0.30, 0.50, 0.45, 1
            on_release: app.start_money_generation()
        MDLabel:
            id: m_status_label
            text: ""
            theme_text_color: "Custom"
            text_color: 0.60, 0.58, 0.70, 1
            font_style: "Caption"
'''

FORM_VIRAL = '''
MDScrollView:
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "14dp"
        size_hint_y: None
        height: self.minimum_height
        MDCard:
            orientation: "vertical"
            padding: "16dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "260dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDBoxLayout:
                orientation: "vertical"
                spacing: "14dp"
                MDTextField:
                    id: v_hook_input
                    hint_text: "Мемная фраза / хук"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.85, 0.5, 0.3, 1
                MDTextField:
                    id: v_genre_input
                    hint_text: "Трендовый жанр"
                    text: "Drift Phonk"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.85, 0.5, 0.3, 1
                MDTextField:
                    id: v_duration_input
                    hint_text: "Время (15/30/45/60)"
                    text: "30"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.85, 0.5, 0.3, 1
        MDRaisedButton:
            text: "Создать вирусный дроп"
            size_hint_x: 1
            elevation: 0
            md_bg_color: 0.78, 0.46, 0.20, 1
            on_release: app.start_viral_generation()
        MDCard:
            orientation: "vertical"
            padding: "14dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "130dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDLabel:
                id: v_status_label
                text: "Ожидание..."
                theme_text_color: "Custom"
                text_color: 0.74, 0.72, 0.84, 1
            MDProgressBar:
                id: v_progress
                value: 0
                max: 100
'''

FORM_ALBUM = '''
MDScrollView:
    MDBoxLayout:
        orientation: "vertical"
        padding: "16dp"
        spacing: "14dp"
        size_hint_y: None
        height: self.minimum_height
        MDCard:
            orientation: "vertical"
            padding: "16dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "360dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDBoxLayout:
                orientation: "vertical"
                spacing: "14dp"
                MDTextField:
                    id: alb_theme_input
                    hint_text: "Концепция альбома"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.42, 0.9, 1
                MDTextField:
                    id: alb_genre_input
                    hint_text: "Жанр"
                    text: "melodic drum and bass"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.42, 0.9, 1
                MDTextField:
                    id: alb_count_input
                    hint_text: "Треков (3 или 4)"
                    text: "3"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.42, 0.9, 1
                MDTextField:
                    id: alb_duration_input
                    hint_text: "Длительность трека (сек)"
                    text: "90"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.42, 0.9, 1
                MDTextField:
                    id: alb_extra_idea
                    hint_text: "Идея дополнительного трека"
                    mode: "rectangle"
                    line_color_normal: 0.26, 0.24, 0.36, 1
                    line_color_focus: 0.62, 0.42, 0.9, 1
        MDBoxLayout:
            spacing: "8dp"
            size_hint_y: None
            height: "46dp"
            MDRaisedButton:
                text: "Свести EP"
                size_hint_x: 1
                elevation: 0
                md_bg_color: 0.50, 0.32, 0.75, 1
                on_release: app.start_album_generation()
            MDRaisedButton:
                text: "Трек в альбом"
                size_hint_x: 1
                elevation: 0
                md_bg_color: 0.24, 0.50, 0.46, 1
                on_release: app.show_add_track_dialog()
        MDCard:
            orientation: "vertical"
            padding: "14dp"
            radius: [24, 24, 24, 24]
            elevation: 0
            size_hint_y: None
            height: "110dp"
            md_bg_color: 0.101, 0.098, 0.156, 1
            MDLabel:
                id: alb_status_label
                text: "Ожидание..."
                theme_text_color: "Custom"
                text_color: 0.74, 0.72, 0.84, 1
'''

# ==============================================================================
# MAIN APP CLASS (FULL RESTORE)
# ==============================================================================
class LemusStudioApp(MDApp):
    def build(self):
        _log("build()")
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "DeepPurple"
        self.theme_cls.accent_palette = "Amber"
        self.current_voice_sample = None
        self.active_sound = None
        self.onboard_idx = 0
        self.onboard_dialog = None
        self.monetize_idx = 0
        self.monetize_dialog = None
        self.file_manager_purpose = "voice"
        self.current_filter = "all"
        self.diag_dialog = None
        self.export_dialog = None
        self.add_track_dialog = None
        self.update_dialog = None
        self.selection_mode = False
        self.selected_ids = set()
        self.last_rendered_items = []
        self.play_queue_list = []
        self.play_idx = 0
        self._pos_event = None
        self.modes = {}
        self.current_mode = "single"
        self._last_gen = None
        self._enhanced_cache = {}
        self._gemini_cache = {}
        self._key_state = {}

        self.file_manager = MDFileManager(
            exit_manager=self.exit_file_manager,
            select_path=self.on_file_selected,
        )
        self.load_config()
        self.load_projects()
        self.load_history()
        return Builder.load_string(KV)

    def on_start(self):
        _log("on_start()")
        def safe(fn, name):
            try:
                fn()
                _log(f"on_start: {name} ок")
            except Exception as e:
                _log(f"on_start: {name} ОШИБКА: {str(e)[:100]}")
        safe(lambda: self.modes.update({
            "single": Builder.load_string(FORM_SINGLE),
            "prompt": Builder.load_string(FORM_PROMPT),
            "viral": Builder.load_string(FORM_VIRAL),
            "album": Builder.load_string(FORM_ALBUM),
        }), "формы")
        safe(lambda: self.switch_mode("single"), "режим")
        safe(self._request_runtime_permissions, "разрешения")
        safe(self._request_all_files_access, "доступ_к_файлам")
        safe(self.populate_settings_fields, "поля_настроек")
        safe(self.refresh_projects_ui, "медиатека")
        safe(lambda: setattr(self.root.ids.version_label, "text", f"версия {CURRENT_VERSION}"), "версия")
        safe(lambda: setattr(self.root.ids.ai_disclose_switch, "active",
             bool(self.config.get("disclose_ai", True))), "переключатель_ai")
        safe(self.update_activation_status, "статус")
        safe(lambda: self.check_for_updates(silent=True), "обновления")
        safe(lambda: self.root.ids.bottom_nav.bind(current=self._on_tab_change), "подсказки")
        _log("on_start() завершён")

    def _on_tab_change(self, nav, name):
        seen = self.config.get("hints_seen")
        if not isinstance(seen, dict):
            seen = {}
            self.config["hints_seen"] = seen
        if name in HINTS and not seen.get(name):
            seen[name] = True
            self.save_config_to_disk()
            toast(HINTS[name])

    def switch_mode(self, mode):
        self.current_mode = mode
        colors = {"single": (0.42, 0.34, 0.78, 1), "prompt": (0.20, 0.56, 0.44, 1),
                  "viral": (0.78, 0.46, 0.20, 1), "album": (0.50, 0.32, 0.75, 1),
                  "money": (0.24, 0.50, 0.46, 1)}
        for m, chip_id in [("single", "chip_single"), ("prompt", "chip_prompt"),
                           ("viral", "chip_viral"), ("album", "chip_album"), ("money", "chip_money")]:
            chip = self.root.ids[chip_id]
            chip.md_bg_color = colors[m] if m == mode else (0.145, 0.138, 0.196, 1)
        area = self.root.ids.studio_area
        area.clear_widgets()
        if mode in self.modes:
            area.add_widget(self.modes[mode])
        seen = self.config.get("hints_seen")
        if not isinstance(seen, dict):
            seen = {}
            self.config["hints_seen"] = seen
        hk = "mode_" + mode
        if hk in HINTS and not seen.get(hk):
            seen[hk] = True
            self.save_config_to_disk()
            toast(HINTS[hk])

    def _mark_key(self, key, status):
        self._key_state[key] = (status, time.time())

    def _key_skippable(self, key):
        st = self._key_state.get(key)
        if not st: return False
        status, ts = st
        if status == "dead" and time.time() - ts < 1800: return True
        return False

    def enhance_single(self):
        w = self.modes["single"].ids
        t = w.s_title_input.text.strip()
        g = w.s_genre_input.text.strip()
        raw = f"{t}. {g}".strip(". ")
        if not raw: toast("Сначала укажи тему!"); return
        self._run_enhance(raw, "single")

    def enhance_prompt_mode(self):
        raw = self.modes["prompt"].ids.p_prompt_input.text.strip()
        if not raw: toast("Сначала напиши идею!"); return
        self._run_enhance(raw, "prompt")

    def _run_enhance(self, raw, mode):
        toast("Улучшаю промпт...")
        def work():
            txt = self._enhance_sync(raw)
            self._enhanced_cache[mode] = txt
            lid = "s_enhanced_label" if mode == "single" else "p_enhanced_label"
            Clock.schedule_once(lambda dt: setattr(self.modes[mode].ids[lid], "text", f"Промпт: {txt}"), 0)
            Clock.schedule_once(lambda dt: toast("Промпт улучшен!"), 0)
        threading.Thread(target=work).start()

    def _enhance_sync(self, raw):
        try:
            res = self._call_llm_text(ENHANCE_SYSTEM + "\nЧерновик: " + raw)
            if res and len(res.strip()) > 20: return res.strip()[:600]
        except Exception as e:
            print(f"enhance llm: {e}")
        return _local_enhance(raw)

    def _gemini_list_models(self, key):
        if key in self._gemini_cache: return self._gemini_cache[key], None
        last_code = None
        try:
            url = ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key=" + urllib.parse.quote(key.strip()))
            req = urllib.request.Request(url, headers={"x-goog-api-key": key.strip()})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            models = []
            for m in data.get("models", []):
                name = m.get("name", "").replace("models/", "")
                if name.startswith("gemini") and "generateContent" in str(m.get("supportedGenerationMethods", [])):
                    if not any(x in name for x in ("embedding", "image", "tts", "audio", "vision", "live")):
                        models.append(name)
            models.sort(key=lambda m: (0 if "flash" in m else 1))
            self._gemini_cache[key] = models
            return models, None
        except urllib.error.HTTPError as e:
            last_code = e.code
        except Exception:
            last_code = 0
        return None, last_code

    def _gemini_post(self, prompt, key, model, mime_json=True):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": key.strip()}
        gc = {"temperature": 0.8}
        if mime_json: gc["response_mime_type"] = "application/json"
        else: gc["temperature"] = 0.9
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gc}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
        return res["candidates"][0]["content"]["parts"][0]["text"]

    def _call_gemini_native(self, prompt, api_key, model=None):
        api_models, code = self._gemini_list_models(api_key)
        if code in (401, 403, 400):
            self._mark_key(api_key, "dead"); raise RuntimeError(f"Gemini dead: {code}")
        if code in (429, 503):
            self._mark_key(api_key, "temp"); raise RuntimeError(f"Gemini temp: {code}")
        models_to_try = []
        if model: models_to_try.append(model)
        if api_models: models_to_try.extend(api_models[:8])
        for m in GEMINI_MODELS:
            if m not in models_to_try: models_to_try.append(m)
        last_err = None
        for m in models_to_try[:10]:
            try:
                text = self._gemini_post(prompt, api_key, m, mime_json=True)
                self._mark_key(api_key, "ok")
                return self._clean_json(text)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 404: continue
                if e.code in (401, 403): self._mark_key(api_key, "dead"); break
                if e.code in (429, 503): self._mark_key(api_key, "temp"); time.sleep(1); break
                if e.code == 400: continue
                break
            except Exception as e:
                last_err = e; break
        if last_err: raise last_err
        raise RuntimeError("Gemini: модели недоступны")

    def _call_llm_text(self, prompt):
        g_keys = self.config.get("gemini_keys", [])
        if not g_keys and self.config.get("gemini_key"): g_keys = [self.config.get("gemini_key")]
        for key in g_keys:
            if not key or self._key_skippable(key): continue
            models, code = self._gemini_list_models(key)
            if code in (401, 403, 400): self._mark_key(key, "dead"); continue
            if code in (429, 503): self._mark_key(key, "temp"); continue
            cand = (models or [])[:5] or GEMINI_MODELS[:5]
            for m in cand:
                try: return self._gemini_post(prompt, key, m, mime_json=False)
                except urllib.error.HTTPError as e:
                    if e.code == 404: continue
                    if e.code in (401, 403): self._mark_key(key, "dead"); break
                    if e.code in (429, 503): self._mark_key(key, "temp"); time.sleep(1); break
                    if e.code == 400: continue
                    break
                except Exception: break
        or_key = self.config.get("openrouter_key", "").strip()
        if or_key:
            for om in OR_MODELS:
                try:
                    headers = {"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"}
                    payload = json.dumps({"model": om, "messages": [{"role": "system", "content": ENHANCE_SYSTEM}, {"role": "user", "content": prompt}]}).encode("utf-8")
                    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=60) as r:
                        res = json.loads(r.read().decode())
                    return res["choices"][0]["message"]["content"]
                except Exception: continue
        return _local_enhance(prompt)

    def make_variation(self):
        if not self._last_gen: toast("Сначала создай трек — потом вариацию"); return
        g = self._last_gen
        toast("Делаю вариацию 2...")
        threading.Thread(target=self._worker_generic, args=(g["prompt"] + " (новая вариация, другое настроение)", g["genre"], g["duration"], g["rtype"], None, None)).start()

    def _go_to_library(self):
        try:
            self.root.ids.bottom_nav.current = "tab_projects"
            self.refresh_projects_ui()
        except Exception as e: _log(f"переход в медиатеку: {e}")

    def _request_runtime_permissions(self):
        if platform != "android": return
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE, Permission.RECORD_AUDIO, Permission.POST_NOTIFICATIONS])
        except Exception as e: _log(f"разрешения: {e}")

    def _request_all_files_access(self):
        if platform != "android": return
        try:
            from jnius import autoclass
            Environment = autoclass("android.os.Environment")
            if Environment.isExternalStorageManager(): return
            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            if activity:
                intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                intent.setData(Uri.parse("package:" + activity.getPackageName()))
                activity.startActivity(intent)
        except Exception as e: _log(f"доступ к файлам: {e}")

    def get_data_path(self):
        path = os.path.join(get_storage_root(), ".data")
        try: os.makedirs(path, exist_ok=True)
        except Exception: path = "."
        return path

    def _migrate_old_data(self):
        sources = [self.user_data_dir, get_storage_root(), "."]
        p = _android_private()
        if p: sources.append(p)
        for src_dir in sources:
            for fname in [CONFIG_FILE, PROJECTS_FILE, HISTORY_FILE, ONBOARDING_FLAG]:
                for cand in [os.path.join(src_dir, fname), os.path.join(src_dir, ".data", fname)]:
                    new = os.path.join(self.get_data_path(), fname)
                    try:
                        if os.path.exists(cand) and cand != new and not os.path.exists(new): shutil.copy2(cand, new)
                    except Exception: pass

    def load_config(self):
        self._migrate_old_data()
        p = os.path.join(self.get_data_path(), CONFIG_FILE)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f: self.config = json.load(f)
                return
            except Exception: pass
        self.config = EMPTY_CONFIG.copy()

    def save_config_to_disk(self):
        try:
            with open(os.path.join(self.get_data_path(), CONFIG_FILE), "w", encoding="utf-8") as f: json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e: toast(f"Ошибка конфига: {str(e)[:40]}")

    def load_projects(self):
        p = os.path.join(self.get_data_path(), PROJECTS_FILE)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f: self.projects = json.load(f)
                return
            except Exception: pass
        self.projects = []

    def save_projects(self):
        try:
            with open(os.path.join(self.get_data_path(), PROJECTS_FILE), "w", encoding="utf-8") as f: json.dump(self.projects, f, ensure_ascii=False, indent=2)
        except Exception as e: toast(f"Ошибка базы: {str(e)[:40]}")

    def load_history(self):
        p = os.path.join(self.get_data_path(), HISTORY_FILE)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f: self.history = json.load(f)
                return
            except Exception: pass
        self.history = []

    def save_history(self):
        try:
            with open(os.path.join(self.get_data_path(), HISTORY_FILE), "w", encoding="utf-8") as f: json.dump(self.history[-20:], f, ensure_ascii=False, indent=2)
        except Exception: pass

    def _player_show(self): Animation(height=dp(68), d=0.22, t="out_quad").start(self.root.ids.player_bar)
    def _player_hide(self): Animation(height=dp(0), d=0.18, t="in_quad").start(self.root.ids.player_bar)

    @staticmethod
    def _fmt(sec):
        try:
            sec = int(sec or 0); return f"{sec // 60}:{sec % 60:02d}"
        except Exception: return "0:00"

    def seek_ratio(self, ratio):
        s = self.active_sound
        if not s: return
        try:
            length = s.length or 0
            if length > 0:
                s.seek(max(0.0, min(1.0, ratio)) * length)
                self.root.ids.player_pos.text = self._fmt(ratio * length)
        except Exception: toast("Перемотка недоступна для этого файла")

    def play_item(self, item):
        if item in self.last_rendered_items: idx = self.last_rendered_items.index(item)
        else: idx = 0; self.last_rendered_items = [item]
        self.play_queue_list = self.last_rendered_items
        self.play_idx = idx
        self._play_current()

    def _play_current(self):
        if not self.play_queue_list: return
        item = self.play_queue_list[self.play_idx % len(self.play_queue_list)]
        path = item.get("mp3_path")
        if not path or not os.path.exists(path): toast("Файл не найден"); return
        if self.active_sound:
            try: self.active_sound.stop()
            except Exception: pass
        self.active_sound = SoundLoader.load(path)
        if self.active_sound:
            self.active_sound.play()
            self._player_show()
            self.root.ids.player_title.text = item.get("title", "")
            self.root.ids.player_artist.text = item.get("genre", "")
            self.root.ids.player_play_btn.icon = "pause"
            cov = item.get("cover_path")
            img = self.root.ids.player_cover
            if cov and os.path.exists(cov): img.source = cov; img.opacity = 1
            else: img.opacity = 0
            self.root.ids.player_seek.value = 0.0
            if self._pos_event is None: self._pos_event = Clock.schedule_interval(self._update_player_pos, 0.4)

    def _update_player_pos(self, dt):
        s = self.active_sound
        if not s: return False
        try:
            pos = s.position or 0; length = s.length or 0
            self.root.ids.player_pos.text = self._fmt(pos)
            sl = self.root.ids.player_seek
            if not sl._drag and length > 0: sl.value = min(1.0, pos / length)
        except Exception: pass
        return True

    def player_toggle(self):
        s = self.active_sound
        if not s: return
        try:
            if s.state == "play": s.stop(); self.root.ids.player_play_btn.icon = "play"
            else: s.play(); self.root.ids.player_play_btn.icon = "pause"
        except Exception: pass

    def player_next(self):
        if not self.play_queue_list: return
        self.play_idx = (self.play_idx + 1) % len(self.play_queue_list); self._play_current()

    def player_prev(self):
        if not self.play_queue_list: return
        self.play_idx = (self.play_idx - 1) % len(self.play_queue_list); self._play_current()

    def player_stop(self):
        if self.active_sound:
            try: self.active_sound.stop()
            except Exception: pass
        if self._pos_event: Clock.unschedule(self._pos_event); self._pos_event = None
        self._player_hide()
        self.root.ids.player_title.text = ""; self.root.ids.player_pos.text = "0:00"; self.root.ids.player_seek.value = 0

    def download_current(self):
        if not self.play_queue_list: toast("Сначала включи трек"); return
        item = self.play_queue_list[self.play_idx % len(self.play_queue_list)]; self.download_project(item)

    def download_project(self, item):
        src = item.get("mp3_path")
        if not src or not os.path.exists(src): toast("Файл не найден"); return
        dest_dir = None
        if platform == "android":
            for cand in ["/storage/emulated/0/Download", "/storage/emulated/0/Music/LemusStudio"]:
                try:
                    os.makedirs(cand, exist_ok=True); t = os.path.join(cand, ".test"); open(t, "w").write("1"); os.remove(t); dest_dir = cand; break
                except Exception: continue
        if not dest_dir: dest_dir = get_storage_root()
        dst = os.path.join(dest_dir, os.path.basename(src))
        try: shutil.copy2(src, dst); toast(f"Сохранено: {dst}")
        except Exception as e: toast(f"Ошибка сохранения: {str(e)[:40]}")

    def _build_plan_prompt(self, base_desc, genre, duration, vocals_hint=""):
        return (f"You are a pro music producer. Task: track {duration}s. Desc: {base_desc}. Genre: {genre}. Vocals: {vocals_hint or 'auto'}. "
                "Return STRICT JSON: {\"title\": str, \"bpm\": int, \"key\": str, \"vocals\": str, \"style_tags\": [str], "
                "\"sections\": [{\"name\":str, \"bars\":int, \"energy\":float}], \"lyrics\": str, \"music_prompt\": str, \"cover_prompt\": str}.")

    def start_single_generation(self):
        w = self.modes["single"].ids
        t = w.s_title_input.text.strip(); g = w.s_genre_input.text.strip(); d = w.s_duration_input.text.strip() or "90"
        if not t: toast("Укажи тему!"); return
        raw = f"{t}. {g}"
        self.add_to_history("Сингл", f"{t} / {g} / {d}с")
        w.s_status_label.text = "Улучшаю промпт и пишу план..."
        self._last_gen = {"genre": g, "duration": int(d), "rtype": "Сингл", "raw": raw, "mode": "single"}
        threading.Thread(target=self._worker_track, args=(raw, g, int(d), "Сингл", w.s_status_label, w.s_progress)).start()

    def start_prompt_generation(self):
        w = self.modes["prompt"].ids
        raw = w.p_prompt_input.text.strip(); dur = int(w.p_duration_input.text.strip() or "120")
        if not raw: toast("Опиши свою идею!"); return
        self.add_to_history("Промпт", f"{raw[:100]} / {dur}с")
        w.p_status_label.text = "Улучшаю промпт и пишу план..."
        self._last_gen = {"genre": raw[:40], "duration": dur, "rtype": "Промпт", "raw": raw, "mode": "prompt"}
        threading.Thread(target=self._worker_generic, args=(raw, raw, dur, "Промпт", w.p_status_label, w.p_progress)).start()

    def start_viral_generation(self):
        w = self.modes["viral"].ids
        h = w.v_hook_input.text.strip(); g = w.v_genre_input.text.strip(); d = w.v_duration_input.text.strip() or "30"
        if not h: toast("Укажи хук!"); return
        raw = f"Вирусный хит TikTok/Reels. Хук: '{h}'. Жанр: {g}."
        self.add_to_history("Хит", f"Хук: {h} / {g} / {d}с")
        w.v_status_label.text = "Улучшаю промпт и пишу план..."
        self._last_gen = {"genre": g, "duration": int(d), "rtype": "Хит", "raw": raw, "mode": "viral"}
        threading.Thread(target=self._worker_generic, args=(raw, g, int(d), "Хит", w.v_status_label, w.v_progress)).start()

    def start_money_generation(self):
        w = self.modes["prompt"].ids
        n = w.m_niche_input.text.strip(); d = w.m_duration_input.text.strip() or "150"
        if not n: toast("Укажи нишу!"); return
        raw = f"Фоновый монетизируемый трек для стримингов. Ниша: '{n}'. Loop-friendly, без резких пиков."
        self.add_to_history("Фон", f"Ниша: {n} / {d}с")
        w.m_status_label.text = "Улучшаю промпт и пишу план..."
        self._last_gen = {"genre": n, "duration": int(d), "rtype": "Фон", "raw": raw, "mode": "prompt"}
        threading.Thread(target=self._worker_generic, args=(raw, n, int(d), "Фон", w.m_status_label, None)).start()

    def start_album_generation(self):
        w = self.modes["album"].ids
        theme = w.alb_theme_input.text.strip(); genre = w.alb_genre_input.text.strip()
        cnt = int(w.alb_count_input.text.strip() or "3"); dur = int(w.alb_duration_input.text.strip() or "90")
        if not theme: toast("Укажи концепцию!"); return
        self.add_to_history("Альбом", f"{theme} / {genre} / {cnt}x{dur}с")
        w.alb_status_label.text = "Пишу концепцию EP..."
        threading.Thread(target=self._worker_album, args=(theme, genre, cnt, dur)).start()

    def _worker_track(self, raw, genre, dur, rtype, label, prog):
        demo = f"Голос: {os.path.basename(self.current_voice_sample)}." if self.current_voice_sample else "Нейро-вокал."
        self._worker_generic(f"{raw} {demo}", genre, dur, rtype, label, prog)

    def _worker_generic(self, raw_desc, genre, duration, rtype, label, prog):
        notes = []
        mode = (self._last_gen or {}).get("mode", self.current_mode)
        try:
            Clock.schedule_once(lambda dt: self._update_progress(label, prog, "Улучшаю промпт...", 8), 0)
            enhanced = self._enhanced_cache.pop(mode, None) or self._enhance_sync(raw_desc)
            Clock.schedule_once(lambda dt: self._show_enhanced(mode, enhanced), 0)
            Clock.schedule_once(lambda dt: self._update_progress(label, prog, "Пишу план трека...", 15), 0)
            prompt = self._build_plan_prompt(enhanced, genre, duration)
            llm = self._producer_with_critic(prompt)
            plan = {"bpm": llm.get("bpm"), "key": llm.get("key"), "sections": llm.get("sections"), "vocals": llm.get("vocals"), "style_tags": llm.get("style_tags")}
            title = llm.get("title", "Трек")
            music_prompt = llm.get("music_prompt", genre)
            lyrics = llm.get("lyrics", "")
            crit = llm.get("_critic") or {}
            total = crit.get("total", "-")
            if not llm.get("_real"):
                notes.append("ключи не ответили — план локальный")
                plan = {"bpm": None, "key": None, "sections": None}
            elif llm.get("_via") == "openrouter": notes.append("текст через OpenRouter")
            elif llm.get("_via") == "gemini": notes.append("текст через Gemini")

            Clock.schedule_once(lambda dt: self._update_progress(label, prog, "Рисуем обложку 3000x3000...", 35), 0)
            cover = self._generate_image(llm.get("cover_prompt", f"{genre} cover"))

            Clock.schedule_once(lambda dt: self._update_progress(label, prog, f"Синтез звука ({duration}с)...", 55), 0)
            audio, audio_src, audio_reasons = self._generate_audio_chunk(music_prompt + ". " + enhanced, duration, plan)
            if audio_src == "arrange": notes.append(f"звук: локальный аранжировщик v4 ({audio_reasons[-1] if audio_reasons else 'no keys'})")
            elif audio_src == "replicate": notes.append("звук: MusicGen Replicate — студийное качество")
            elif audio_src == "hf": notes.append("звук: MusicGen HuggingFace")
            elif audio_src == "poll": notes.append("звук: Pollinations AI")

            if lyrics and self.current_voice_sample:
                Clock.schedule_once(lambda dt: self._update_progress(label, prog, "Клонирование голоса...", 75), 0)
                self._fish_clone(lyrics, self.current_voice_sample)

            Clock.schedule_once(lambda dt: self._update_progress(label, prog, "Сохранение и мастеринг...", 90), 0)
            storage = get_storage_root()
            safe = re.sub(r"[^\w\-]", "_", title)[:60]
            mp3_p = os.path.join(storage, f"{safe}.mp3")
            wav_p = os.path.join(storage, f"{safe}_Master.wav")
            cover_p = os.path.join(storage, f"{safe}_Cover.jpg")

            with open(mp3_p, "wb") as f: f.write(audio)
            self._write_wav(wav_p, audio, duration, seed=title, plan=plan)
            if cover:
                with open(cover_p, "wb") as f: f.write(cover)
            self._inject_id3(mp3_p, title, "Anton Lemus & AI", genre, cover)

            self.projects.insert(0, {
                "id": f"p_{int(time.time()*1000)}", "title": title, "genre": genre, "type": f"{rtype} ({duration}с)",
                "mp3_path": mp3_p, "wav_path": wav_p, "cover_path": cover_p if cover else None,
                "lyrics": lyrics, "bpm": plan.get("bpm"), "key": plan.get("key"),
                "enhanced_prompt": enhanced, "audio_source": audio_src,
                "critic_total": total, "ai_disclose": bool(self.config.get("disclose_ai", True)),
                "date": time.strftime("%Y-%m-%d %H:%M"),
            })
            self.save_projects()
            self._last_gen = {"prompt": prompt, "genre": genre, "duration": duration, "rtype": rtype}

            msg = f"Готово! Критик: {total}/10"
            if plan.get("bpm"): msg += f" • {plan.get('bpm')} BPM • {plan.get('key')}"
            if notes: msg += " (" + "; ".join(notes) + ")"
            Clock.schedule_once(lambda dt: self._update_progress(label, prog, msg, 100), 0)
            Clock.schedule_once(lambda dt: self._go_to_library(), 0)
            Clock.schedule_once(lambda dt: toast(f"{title}: готово! Слушай в Медиатеке"), 0)
            self._send_notification("Lemus Studio", f"{title} готов!")
        except Exception as e:
            traceback.print_exc()
            Clock.schedule_once(lambda dt: self._update_progress(label, prog, f"Ошибка: {str(e)[:60]}", 0), 0)

    def _show_enhanced(self, mode, txt):
        try:
            lid = "s_enhanced_label" if mode == "single" else "p_enhanced_label"
            setattr(self.modes[mode].ids[lid], "text", f"Промпт: {txt}")
        except Exception: pass

    def _update_progress(self, label, prog, text, val):
        if label is not None: label.text = text
        if prog is not None: prog.value = val

    def _worker_album(self, theme, genre, cnt, dur):
        try:
            enhanced = self._enhance_sync(f"Альбом: {theme}. Жанр: {genre}.")
            prompt = (f"EP из {cnt} треков по {dur}с. Тема: '{enhanced}', жанр: {genre}. "
                      "JSON: album_title, cover_prompt, tracks(title, music_prompt, lyrics, bpm, key).")
            llm = self._producer_with_critic(prompt)
            album = llm.get("album_title", "EP")
            album_id = f"alb_{int(time.time())}"
            storage = get_storage_root()
            cover = self._generate_image(llm.get("cover_prompt", f"{genre} album"))
            cover_p = os.path.join(storage, f"{album}_Cover.jpg")
            if cover:
                with open(cover_p, "wb") as f: f.write(cover)
            for i, t in enumerate(llm.get("tracks", [])[:cnt]):
                plan = {"bpm": t.get("bpm"), "key": t.get("key"), "sections": None}
                Clock.schedule_once(lambda dt, x=i: setattr(self.modes["album"].ids.alb_status_label, "text", f"Трек {x+1}/{cnt}..."), 0)
                aud, src, _ = self._generate_audio_chunk(t.get("music_prompt", genre), dur, plan)
                safe = re.sub(r"[^\w]", "_", t.get("title", "track"))[:40]
                mp3_p = os.path.join(storage, f"{album}_0{i+1}_{safe}.mp3")
                wav_p = os.path.join(storage, f"{album}_0{i+1}_Master.wav")
                with open(mp3_p, "wb") as f: f.write(aud)
                self._write_wav(wav_p, aud, dur, seed=album + str(i), plan=plan)
                self._inject_id3(mp3_p, t.get("title", "Track"), album, genre, cover)
                self.projects.insert(0, {
                    "id": f"p_{int(time.time()*1000)}_{i}", "title": f"[{album}] {t.get('title')}", "genre": genre,
                    "type": f"Альбом ({dur}с)", "mp3_path": mp3_p, "wav_path": wav_p, "cover_path": cover_p if cover else None,
                    "album_id": album_id, "album_title": album, "lyrics": t.get("lyrics", ""), "audio_source": src,
                    "ai_disclose": bool(self.config.get("disclose_ai", True)), "date": time.strftime("%Y-%m-%d %H:%M"),
                })
            self.save_projects()
            Clock.schedule_once(lambda dt: setattr(self.modes["album"].ids.alb_status_label, "text", f"Альбом '{album}' готов!"), 0)
            Clock.schedule_once(lambda dt: self._go_to_library(), 0)
        except Exception as e:
            Clock.schedule_once(lambda dt: setattr(self.modes["album"].ids.alb_status_label, "text", f"Ошибка: {str(e)[:80]}"), 0)

    def _worker_add_track(self, album, idea, dur):
        try:
            genre = album.get("genre", ""); theme = idea or f"продолжение альбома '{album['title']}'"
            demo = f"Голос: {os.path.basename(self.current_voice_sample)}." if self.current_voice_sample else ""
            enhanced = self._enhance_sync(f"Трек для альбома '{album['title']}'. Тема: {theme}. {demo}")
            prompt = self._build_plan_prompt(enhanced, genre, dur)
            llm = self._producer_with_critic(prompt)
            plan = {"bpm": llm.get("bpm"), "key": llm.get("key"), "sections": llm.get("sections")}
            title = llm.get("title", "Track")
            audio, src, _ = self._generate_audio_chunk(llm.get("music_prompt", genre), dur, plan)
            storage = get_storage_root()
            safe = re.sub(r"[^\w]", "_", title)[:40]; n = album["tracks"] + 1
            mp3_p = os.path.join(storage, f"{album['title']}_0{n}_{safe}.mp3")
            wav_p = os.path.join(storage, f"{album['title']}_0{n}_Master.wav")
            with open(mp3_p, "wb") as f: f.write(audio)
            self._write_wav(wav_p, audio, dur, seed=title, plan=plan)
            self._inject_id3(mp3_p, title, album["title"], genre, None)
            self.projects.insert(0, {
                "id": f"p_{int(time.time()*1000)}", "title": f"[{album['title']}] {title}", "genre": genre,
                "type": f"Альбом ({dur}с)", "album_id": album["album_id"], "album_title": album["title"],
                "mp3_path": mp3_p, "wav_path": wav_p, "cover_path": album.get("cover_path"),
                "lyrics": llm.get("lyrics", ""), "audio_source": src,
                "ai_disclose": bool(self.config.get("disclose_ai", True)), "date": time.strftime("%Y-%m-%d %H:%M"),
            })
            self.save_projects()
            Clock.schedule_once(lambda dt: setattr(self.modes["album"].ids.alb_status_label, "text", f"'{title}' добавлен!"), 0)
            Clock.schedule_once(lambda dt: self._go_to_library(), 0)
        except Exception as e:
            Clock.schedule_once(lambda dt: setattr(self.modes["album"].ids.alb_status_label, "text", f"Ошибка: {str(e)[:80]}"), 0)

    def get_albums(self):
        albums = {}
        for p in self.projects:
            aid = p.get("album_id")
            if not aid: continue
            if aid not in albums: albums[aid] = {"album_id": aid, "title": p.get("album_title", aid), "genre": p.get("genre", ""), "cover_path": p.get("cover_path"), "tracks": 0}
            albums[aid]["tracks"] += 1
        return list(albums.values())

    def show_add_track_dialog(self):
        albums = self.get_albums()
        if not albums: toast("Сначала создай альбом!"); return
        buttons = [MDFlatButton(text=f"{a['title'][:18]} ({a['tracks']})", on_release=lambda inst, al=a: self._start_add_track(al)) for a in albums[:4]]
        self.add_track_dialog = MDDialog(title="Добавить трек", text="Жанр и обложка возьмутся из альбома:", buttons=buttons)
        self.add_track_dialog.open()

    def _start_add_track(self, album):
        try: self.add_track_dialog.dismiss()
        except Exception: pass
        idea = self.modes["album"].ids.alb_extra_idea.text.strip()
        dur = int(self.modes["album"].ids.alb_duration_input.text.strip() or "90")
        self.modes["album"].ids.alb_status_label.text = "Пишу трек..."
        threading.Thread(target=self._worker_add_track, args=(album, idea, dur)).start()

    def _generate_image(self, prompt):
        try:
            enc = urllib.parse.quote(prompt[:180])
            url = f"https://image.pollinations.ai/prompt/{enc}?width=3000&height=3000&nologo=true"
            req = urllib.request.Request(url, headers={"User-Agent": "LemusStudio/7.6"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = r.read()
                if len(d) > 5000: return d
        except Exception as e: print(f"Cover: {e}")
        return None

    def _generate_audio_replicate(self, prompt, duration, reasons):
        token = (self.config.get("replicate_token") or "").strip()
        if not token: reasons.append("Replicate: no token"); return None
        headers = {"Authorization": f"Token {token}", "Content-Type": "application/json", "Prefer": "wait=60"}
        dur = max(5, min(int(duration or 30), 120))
        for m in REPLICATE_MUSIC_MODELS:
            try:
                inp = {"prompt": prompt[:800], "duration": dur}; inp.update(m.get("extra", {}))
                payload = json.dumps({"input": inp}).encode()
                req = urllib.request.Request(f"https://api.replicate.com/v1/models/{m['owner_model']}/predictions", data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=70) as r: pred = json.loads(r.read().decode())
                status = pred.get("status"); out = pred.get("output"); get_url = (pred.get("urls") or {}).get("get")
                waited = 0
                while status not in ("succeeded", "failed", "canceled") and get_url and waited < 300:
                    time.sleep(5); waited += 5
                    with urllib.request.urlopen(urllib.request.Request(get_url, headers=headers), timeout=20) as r2: pred = json.loads(r2.read().decode())
                    status = pred.get("status"); out = pred.get("output")
                if status == "succeeded" and out:
                    url = out if isinstance(out, str) else (out[-1] if isinstance(out, list) else None)
                    if url:
                        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "LemusStudio/7.6"}), timeout=120) as r3:
                            d = r3.read()
                        if _looks_like_audio(d): return d
                    reasons.append(f"Replicate {m['owner_model']}: not audio")
                else: reasons.append(f"Replicate {m['owner_model']}: status {status}")
            except urllib.error.HTTPError as e:
                body = ""
                try: body = e.read().decode()[:200]
                except Exception: pass
                if e.code == 403 and "1010" in body: reasons.append("Replicate: 403/1010 - billing needed"); return None
                elif e.code == 401: reasons.append("Replicate: 401 - invalid token")
                else: reasons.append(f"Replicate {m['owner_model']}: HTTP {e.code}")
                continue
            except Exception as e: reasons.append(f"Replicate {m['owner_model']}: {str(e)[:60]}"); continue
        return None

    def _generate_audio_chunk(self, prompt, duration, plan=None):
        reasons = []
        d = self._generate_audio_replicate(prompt, duration, reasons)
        if d: return d, "replicate", reasons
        hf = self.config.get("hf_token")
        if hf:
            for url in ["https://router.huggingface.co/hf-inference/models/facebook/musicgen-small", "https://api-inference.huggingface.co/models/facebook/musicgen-small"]:
                try:
                    headers = {"Authorization": f"Bearer {hf}", "Content-Type": "application/json"}
                    payload = json.dumps({"inputs": prompt[:160], "parameters": {"duration": min(duration, 30)}}).encode()
                    req = urllib.request.Request(url, data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=90) as r:
                        dd = r.read()
                        if _looks_like_audio(dd): return dd, "hf", reasons
                    reasons.append("HF: not audio")
                except Exception as e: reasons.append(f"HF: {str(e)[:50]}")
        else: reasons.append("HF: no token")
        try:
            enc = urllib.parse.quote(prompt[:160])
            req = urllib.request.Request(f"https://audio.pollinations.ai/prompt/{enc}", headers={"User-Agent": "LemusStudio/7.6"})
            with urllib.request.urlopen(req, timeout=60) as r:
                dd = r.read()
                if _looks_like_audio(dd): return dd, "poll", reasons
            reasons.append("Pollinations: not audio")
        except Exception as e: reasons.append(f"Pollinations: {str(e)[:50]}")
        
        engine = ProceduralAudioEngineV4()
        return engine.generate(duration, prompt, plan), "arrange", reasons

    def _write_wav(self, path, audio, dur, seed="", plan=None):
        try:
            if audio[:4] == b"RIFF":
                with open(path, "wb") as f: f.write(audio); return
        except Exception: pass
        done = False
        if shutil.which("ffmpeg"):
            try:
                tmp = path + ".tmp"
                with open(tmp, "wb") as f: f.write(audio)
                import subprocess
                r = subprocess.run(["ffmpeg", "-y", "-i", tmp, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try: os.remove(tmp)
                except Exception: pass
                done = (r.returncode == 0 and os.path.exists(path))
            except Exception: pass
        if not done:
            engine = ProceduralAudioEngineV4()
            with open(path, "wb") as f: f.write(engine.generate(dur, seed, plan))

    def _convert_format(self, src, dst, codec_args):
        if not shutil.which("ffmpeg"): return False
        try:
            import subprocess
            r = subprocess.run(["ffmpeg", "-y", "-i", src] + codec_args + [dst], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return r.returncode == 0 and os.path.exists(dst)
        except Exception: return False

    def _inject_id3(self, mp3_path, title, artist, genre, cover):
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TDRC, APIC
            audio = MP3(mp3_path, ID3=ID3)
            try: audio.add_tags()
            except Exception: pass
            audio.tags.add(TIT2(encoding=3, text=title)); audio.tags.add(TPE1(encoding=3, text=artist))
            audio.tags.add(TALB(encoding=3, text=title)); audio.tags.add(TDRC(encoding=3, text="2026"))
            if genre: audio.tags.add(TCON(encoding=3, text=genre))
            if cover: audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            audio.save()
        except Exception as e: print(f"ID3: {e}")

    def _prepare_lyrics_for_tts(self, lyrics):
        if not lyrics: return ""
        t = re.sub(r"\[[^\]]*\]", " ", lyrics); t = t.replace("+", "")
        return re.sub(r"[ \t]+", " ", t).strip()

    def _clean_json(self, text):
        t = text.strip()
        if t.startswith("```"):
            lines = t.splitlines()
            if lines[0].startswith("```"): lines = lines[1:]
            if lines and lines[-1].startswith("```"): lines = lines[:-1]
            t = "\n".join(lines).strip()
        if t.startswith("```json"): t = t[7:]
        if t.endswith("```"): t = t[:-3]
        return json.loads(t.strip())

    def _call_llm_json(self, user_prompt):
        g_keys = self.config.get("gemini_keys", [])
        if not g_keys and self.config.get("gemini_key"): g_keys = [self.config.get("gemini_key")]
        for key in g_keys:
            if not key or self._key_skippable(key): continue
            try:
                res = self._call_gemini_native(user_prompt, key)
                if isinstance(res, dict): res["_real"] = True; res["_via"] = "gemini"; return res
            except urllib.error.HTTPError as e:
                if e.code in (401, 403): self._mark_key(key, "dead")
                elif e.code in (429, 503): self._mark_key(key, "temp")
                continue
            except Exception: continue
        or_key = self.config.get("openrouter_key", "").strip()
        if or_key:
            for om in OR_MODELS:
                try:
                    headers = {"Authorization": f"Bearer {or_key}", "Content-Type": "application/json", "HTTP-Referer": "https://lemus-ai-music-studio.onrender.com", "X-Title": "Lemus Studio"}
                    payload = json.dumps({"model": om, "messages": [{"role": "system", "content": "Valid JSON only."}, {"role": "user", "content": user_prompt}], "response_format": {"type": "json_object"}}).encode("utf-8")
                    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=payload, headers=headers)
                    with urllib.request.urlopen(req, timeout=60) as r:
                        res = json.loads(r.read().decode())
                    out = self._clean_json(res["choices"][0]["message"]["content"]); out["_real"] = True; out["_via"] = "openrouter"; return out
                except Exception as e: print(f"OR {om}: {str(e)[:60]}"); continue
        return {"title": "Instrumental", "music_prompt": "melodic beat", "cover_prompt": "cover", "lyrics": "", "bpm": 110, "key": "A minor", "sections": None, "_real": False}

    def _run_critic(self, meta):
        try:
            return self._call_llm_json(f"Critic. Rate: {json.dumps({k:v for k,v in meta.items() if not k.startswith('_')}, ensure_ascii=False)[:1500]}\nJSON: {{\"scores\":{{}},\"total\":N,\"verdict\":\"release|revise\",\"fixes\":\"\"}}")
        except Exception: return {"scores": {}, "total": 8.0, "verdict": "release", "fixes": ""}

    def _producer_with_critic(self, prompt):
        meta = self._call_llm_json(prompt); real = meta.get("_real")
        if not isinstance(meta, dict) or not meta.get("music_prompt"): return meta
        if real:
            crit = self._run_critic(meta); total = crit.get("total") or 10
            if crit.get("verdict") == "revise" and total < 7.0:
                meta2 = self._call_llm_json(prompt + f"\n\nCRITIC: {crit.get('fixes','')}")
                if isinstance(meta2, dict) and meta2.get("music_prompt"): meta = meta2; crit = self._run_critic(meta)
            meta["_critic"] = crit
        meta["_real"] = real; return meta

    def _fish_clone(self, lyrics, sample_path):
        fish_key = self.config.get("fish_key", "")
        if not fish_key or not sample_path: return None
        clean = self._prepare_lyrics_for_tts(lyrics)
        try:
            with open(sample_path, "rb") as f: sample = f.read()
            b = "----LemusBoundary"; body = io.BytesIO()
            body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"voices\"; filename=\"sample.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n".encode()); body.write(sample); body.write(b"\r\n")
            body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nclone_{int(time.time())}\r\n".encode())
            body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"visibility\"\r\n\r\nprivate\r\n".encode()); body.write(f"--{b}--\r\n".encode())
            req = urllib.request.Request("https://api.fish.audio/v1/models", data=body.getvalue(), headers={"Authorization": f"Bearer {fish_key}", "Content-Type": f"multipart/form-data; boundary={b}"})
            with urllib.request.urlopen(req, timeout=90) as r:
                md = json.loads(r.read().decode()); mid = md.get("_id") or md.get("id")
                if not mid: return None
                payload = json.dumps({"text": clean[:2000], "reference_id": mid, "format": "mp3", "mp3_bitrate": 128}).encode()
                req2 = urllib.request.Request("https://api.fish.audio/v1/tts", data=payload, headers={"Authorization": f"Bearer {fish_key}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req2, timeout=120) as r2:
                    c = r2.read(); return c if len(c) > 5000 else None
        except Exception as e: print(f"Fish: {e}"); return None

    def toggle_selection_mode(self):
        self.selection_mode = not self.selection_mode; self.selected_ids = set()
        self.root.ids.selection_bar.height = dp(56) if self.selection_mode else dp(0); self.refresh_projects_ui()

    def _exit_selection_mode(self):
        self.selection_mode = False; self.selected_ids = set()
        try: self.root.ids.selection_bar.height = dp(0); self.root.ids.selection_count.text = "Selected: 0"
        except Exception: pass

    def _toggle_select(self, item):
        pid = item.get("id") or item.get("mp3_path")
        if pid in self.selected_ids: self.selected_ids.discard(pid)
        else: self.selected_ids.add(pid)
        self.root.ids.selection_count.text = f"Selected: {len(self.selected_ids)}"; self.refresh_projects_ui()

    def refresh_projects_ui(self, filter_text="", filter_type="all"):
        container = self.root.ids.projects_list_container; container.clear_widgets(); rendered = []
        ru = {"Single": "Сингл", "Viral": "Хит", "EP": "Альбом", "Money": "Фон", "Prompt": "Промпт"}
        for item in self.projects:
            title = item.get("title", ""); genre = item.get("genre", ""); itype = item.get("type", "")
            if filter_text and filter_text.lower() not in title.lower() and filter_text.lower() not in genre.lower(): continue
            if filter_type != "all" and not (itype.startswith(ru.get(filter_type, filter_type)) or filter_type in itype): continue
            rendered.append(item); pid = item.get("id") or item.get("mp3_path")
            extra = f" • {item.get('date','')}"
            if item.get("bpm"): extra += f" • {item.get('bpm')} BPM"
            if item.get("audio_source") in ("replicate", "hf", "poll"): extra += " • AI-sound"
            li = TwoLineAvatarIconListItem(text=title, secondary_text=f"{genre} • {itype}{extra}")
            if self.selection_mode:
                cb = CheckboxLeftWidget(active=pid in self.selected_ids); cb.bind(active=lambda a, v, it=item: self._toggle_select(it)); li.add_widget(cb)
            else:
                ic_play = IconLeftWidget(icon="play-circle"); ic_play.bind(on_release=lambda x, it=item: self.play_item(it)); li.add_widget(ic_play)
                ic_dl = IconRightWidget(icon="download"); ic_dl.bind(on_release=lambda x, it=item: self.download_project(it)); li.add_widget(ic_dl)
                ic_exp = IconRightWidget(icon="export"); ic_exp.bind(on_release=lambda x, it=item: self.show_export_formats(it)); li.add_widget(ic_exp)
                ic_share = IconRightWidget(icon="share-variant"); ic_share.bind(on_release=lambda x, it=item: self.share_project(it)); li.add_widget(ic_share)
                ic_del = IconRightWidget(icon="delete"); ic_del.bind(on_release=lambda x, it=item: self.confirm_delete([it])); li.add_widget(ic_del)
            container.add_widget(li)
        self.last_rendered_items = rendered

    def filter_projects(self, text): self.refresh_projects_ui(filter_text=text, filter_type=self.current_filter)
    def filter_by_type(self, ftype): self.current_filter = ftype; self.refresh_projects_ui(filter_text=self.root.ids.search_input.text, filter_type=ftype)

    def confirm_delete(self, items):
        n = len(items); dlg = None
        def do_delete(i):
            try: dlg.dismiss()
            except Exception: pass
            for item in items:
                if item in self.projects:
                    for p in [item.get("mp3_path"), item.get("wav_path"), item.get("cover_path")]:
                        if p and os.path.exists(p):
                            try: os.remove(p)
                            except Exception: pass
                    self.projects.remove(item)
            self.save_projects(); self._exit_selection_mode(); self.refresh_projects_ui(); toast(f"Deleted: {n}")
        dlg = MDDialog(title="Confirm", text=f"Delete {n} tracks?", buttons=[MDFlatButton(text="Cancel", on_release=lambda i: dlg.dismiss()), MDRaisedButton(text="Delete", md_bg_color=(0.68, 0.24, 0.24, 1), on_release=do_delete)])
        dlg.open()

    def delete_selected(self):
        if not self.selected_ids: toast("Nothing selected"); return
        items = [p for p in self.projects if (p.get("id") or p.get("mp3_path")) in self.selected_ids]; self.confirm_delete(items)

    def share_project(self, item):
        mp3 = item.get("mp3_path")
        if not mp3 or not os.path.exists(mp3): toast("File not found"); return
        if platform == "android":
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent"); Uri = autoclass("android.net.Uri"); File = autoclass("java.io.File"); PythonActivity = autoclass("org.kivy.android.PythonActivity")
                intent = Intent(Intent.ACTION_SEND); intent.setType("audio/mpeg"); intent.putExtra(Intent.EXTRA_STREAM, Uri.fromFile(File(mp3))); intent.putExtra(Intent.EXTRA_SUBJECT, item.get("title", "Track"))
                PythonActivity.mActivity.startActivity(Intent.createChooser(intent, "Share"))
            except Exception as e: toast(f"Share err: {str(e)[:50]}")
        else: toast(f"File: {mp3}")

    def show_export_formats(self, item):
        wav = item.get("wav_path"); has_ff = bool(shutil.which("ffmpeg"))
        lines = ["Formats:", "WAV 44.1/16", "MP3 320", ("FLAC" if has_ff else "FLAC (need ffmpeg)"), ("MP3 256" if has_ff else "MP3 256 (need ffmpeg)"), "ZIP", "Download"]
        buttons = [MDRaisedButton(text="Download", md_bg_color=(0.30, 0.50, 0.45, 1), on_release=lambda i, it=item: (self._dismiss_export(), self.download_project(it))),
                   MDRaisedButton(text="ZIP", on_release=lambda i, it=item: (self._dismiss_export(), self.export_project_zip(it))),
                   MDFlatButton(text="Close", on_release=lambda i: self._dismiss_export())]
        if has_ff and wav: buttons.insert(0, MDRaisedButton(text="FLAC+m4a", md_bg_color=(0.3, 0.6, 0.5, 1), on_release=lambda i, it=item: (self._dismiss_export(), self._export_extra(it))))
        self.export_dialog = MDDialog(title="Export", text="\n".join(lines), buttons=buttons); self.export_dialog.open()

    def _dismiss_export(self):
        try: self.export_dialog.dismiss()
        except Exception: pass

    def _export_extra(self, item):
        wav = item.get("wav_path"); mp3 = item.get("mp3_path"); base = os.path.splitext(wav or mp3)[0]; made = []
        if wav:
            if self._convert_format(wav, base + ".flac", ["-c:a", "flac"]): made.append("FLAC")
            if self._convert_format(wav, base + ".m4a", ["-c:a", "aac", "-b:a", "256k"]): made.append("M4A")
        if mp3:
            if self._convert_format(mp3, base + "_256.mp3", ["-b:a", "256k"]): made.append("MP3-256")
            if self._convert_format(mp3, base + "_192.mp3", ["-b:a", "192k"]): made.append("MP3-192")
        toast("Created: " + (", ".join(made) if made else "nothing"))

    def export_project_zip(self, item):
        try:
            storage = get_storage_root(); title = re.sub(r"[^\w]", "_", item.get("title", "track"))[:40]; zip_p = os.path.join(storage, f"{title}_dist.zip")
            disclose = bool(item.get("ai_disclose", self.config.get("disclose_ai", True)))
            with zipfile.ZipFile(zip_p, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in [item.get("mp3_path"), item.get("wav_path"), item.get("cover_path")]:
                    if p and os.path.exists(p): zf.write(p, os.path.basename(p))
                passport = {k: v for k, v in item.items() if not k.endswith("_path")}
                passport["distributor_note"] = "AI generated." if disclose else "Human produced."
                zf.writestr("passport.json", json.dumps(passport, ensure_ascii=False, indent=2))
                zf.writestr("metadata.csv", f"title,artist,genre\n\"{item.get('title','')}\",\"Anton Lemus\",\"{item.get('genre','')}\"")
                zf.writestr("press_release.txt", f"PRESS RELEASE\n\n{item.get('title','')} by Anton Lemus.")
            toast(f"ZIP ready: {os.path.basename(zip_p)}")
        except Exception as e: toast(f"ZIP err: {str(e)[:50]}")

    def _import_keys_from_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f: data = json.load(f)
            if not isinstance(data, dict): toast("Invalid JSON"); return
            if not any(k in data for k in ["gemini_keys", "openrouter_key", "replicate_token"]): toast("No keys"); return
            for key in ["gemini_keys", "gemini_key", "gemini_model", "openrouter_key", "yandex_token", "hf_token", "fish_key", "groq_key", "disclose_ai", "replicate_token"]:
                if key in data: self.config[key] = data[key]
            keys = self.config.get("gemini_keys", [])
            if isinstance(keys, str): keys = [keys]
            self.config["gemini_keys"] = [k for k in keys if k]
            if self.config["gemini_keys"] and not self.config.get("gemini_key"): self.config["gemini_key"] = self.config["gemini_keys"][0]
            self.config["is_master_activated"] = True; self.config["activated_at"] = time.strftime("%Y-%m-%d %H:%M"); self.config["activation_source"] = "json"
            self._key_state = {}; self._gemini_cache = {}; self.save_config_to_disk(); self.populate_settings_fields(); self.update_activation_status()
            toast(f"Keys loaded! Gemini: {len(self.config.get('gemini_keys', []))}")
        except json.JSONDecodeError: toast("JSON parse error")
        except Exception as e: toast(f"Error: {str(e)[:50]}")

    def unlock_master_keys(self):
        pwd = self.root.ids.master_password_input.text.strip()
        if hashlib.sha256(pwd.encode()).hexdigest() != MASTER_HASH: toast("Wrong password!"); return
        toast("Loading gist..."); threading.Thread(target=self._fetch_remote_keys_thread).start()

    def _fetch_remote_keys_thread(self):
        try:
            req = urllib.request.Request(REMOTE_KEYS_URL, headers={"User-Agent": "LemusStudio/7.6", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r: remote = json.loads(r.read().decode("utf-8"))
            for k, v in remote.items(): self.config[k] = v
            self.config["is_master_activated"] = True; self.config["activated_at"] = time.strftime("%Y-%m-%d %H:%M"); self.config["activation_source"] = "gist"
            self._key_state = {}; self._gemini_cache = {}; self.save_config_to_disk()
            Clock.schedule_once(lambda dt: self._on_keys_loaded(True), 0)
        except Exception as e: Clock.schedule_once(lambda dt: self._on_keys_loaded(False, str(e)), 0)

    def _on_keys_loaded(self, ok, err=""):
        if ok: self.populate_settings_fields(); self.update_activation_status(); self.root.ids.master_password_input.text = ""; toast("Activated!")
        else: toast(f"Gist error: {err[:60]}")

    def reset_all_keys(self):
        self.config = EMPTY_CONFIG.copy(); self._key_state = {}; self._gemini_cache = {}; self.save_config_to_disk(); self.populate_settings_fields(); self.update_activation_status(); toast("Keys reset")

    def set_disclose_ai(self, active):
        self.config["disclose_ai"] = bool(active); self.save_config_to_disk(); toast("AI status: " + ("on" if active else "off"))

    def update_activation_status(self):
        label = self.root.ids.activation_status
        if self.config.get("is_master_activated"):
            label.text = f"Active\n({self.config.get('activation_source','?')})"; label.theme_text_color = "Custom"; label.text_color = (0.35, 0.85, 0.45, 1)
        elif self.config.get("gemini_key") or self.config.get("openrouter_key") or self.config.get("replicate_token"):
            label.text = "Guest mode"; label.theme_text_color = "Custom"; label.text_color = (0.9, 0.75, 0.3, 1)
        else:
            label.text = "Not activated"; label.theme_text_color = "Custom"; label.text_color = (0.62, 0.60, 0.70, 1)

    def save_user_settings(self):
        r = self.root; uk = r.ids.cfg_gemini.text.strip(); keys = list(self.config.get("gemini_keys", []))
        if uk:
            if uk not in keys: keys.insert(0, uk)
            self.config["gemini_key"] = uk
        elif keys: self.config["gemini_key"] = keys[0]
        else: self.config["gemini_key"] = ""
        self.config["gemini_keys"] = keys
        self.config["replicate_token"] = r.ids.cfg_replicate.text.strip() or self.config.get("replicate_token", "")
        self.config["openrouter_key"] = r.ids.cfg_openrouter.text.strip() or self.config.get("openrouter_key", "")
        self.config["yandex_token"] = r.ids.cfg_yandex.text.strip() or self.config.get("yandex_token", "")
        self.config["hf_token"] = r.ids.cfg_hf.text.strip() or self.config.get("hf_token", "")
        self.config["fish_key"] = r.ids.cfg_fish.text.strip() or self.config.get("fish_key", "")
        self.config["groq_key"] = r.ids.cfg_groq.text.strip() or self.config.get("groq_key", "")
        self._key_state = {}; self._gemini_cache = {}; self.save_config_to_disk(); self.update_activation_status()
        toast(f"Saved! Gemini: {len(keys)}")

    def populate_settings_fields(self):
        r = self.root
        r.ids.cfg_gemini.text = self.config.get("gemini_key", ""); r.ids.cfg_openrouter.text = self.config.get("openrouter_key", "")
        r.ids.cfg_yandex.text = self.config.get("yandex_token", ""); r.ids.cfg_hf.text = self.config.get("hf_token", "")
        r.ids.cfg_fish.text = self.config.get("fish_key", ""); r.ids.cfg_groq.text = self.config.get("groq_key", "")
        r.ids.cfg_replicate.text = self.config.get("replicate_token", "")

    def show_vault_status(self):
        if self.config.get("is_master_activated"): toast(f"LemusAI: Gemini {len(self.config.get('gemini_keys', []))}")
        elif self.config.get("gemini_key") or self.config.get("openrouter_key") or self.config.get("replicate_token"): toast("Guest mode")
        else: toast("No keys")

    def run_key_diagnostics(self):
        toast("Diagnostics..."); threading.Thread(target=self._diag_thread).start()

    def _diag_thread(self):
        from concurrent.futures import ThreadPoolExecutor
        lines = ["Diagnostics v7.6.0:"]
        try:
            req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?key=invalid_test", headers={"User-Agent": "LemusStudio/7.6"})
            try: urllib.request.urlopen(req, timeout=10); lines.append("Network/SSL: OK")
            except urllib.error.HTTPError: lines.append("Network/SSL: OK")
        except Exception as e: lines.append(f"Network/SSL: ERROR {str(e)[:60]}")
        
        g_keys = self.config.get("gemini_keys", [])
        if not g_keys and self.config.get("gemini_key"): g_keys = [self.config.get("gemini_key")]
        
        def ping(k):
            models, code = self._gemini_list_models(k)
            if code in (401, 403, 400): self._mark_key(k, "dead"); return ("dead", f"HTTP {code}")
            if code in (429, 503): self._mark_key(k, "temp"); return ("temp", f"HTTP {code}")
            if not models: return ("dead", "no models")
            for m in models[:5]:
                try: self._gemini_post("ping", k, m, mime_json=False); self._mark_key(k, "ok"); return ("ok", f"{m} ({len(models)})")
                except urllib.error.HTTPError as e:
                    if e.code == 404: continue
                    if e.code in (401, 403): self._mark_key(k, "dead"); return ("dead", f"HTTP {e.code}")
                    if e.code in (429, 503): self._mark_key(k, "temp"); return ("temp", f"HTTP {e.code}")
                    break
                except Exception as e: break
            return ("dead", "gen unavailable")
        
        try:
            with ThreadPoolExecutor(max_workers=6) as ex: results = list(ex.map(ping, g_keys))
        except Exception: results = [ping(k) for k in g_keys]
        
        alive = 0; temp = 0
        for i, (k, res) in enumerate(zip(g_keys, results)):
            status, detail = res
            if status == "ok": alive += 1; lines.append(f"Gemini #{i+1}: OK - {detail}")
            elif status == "temp": temp += 1; lines.append(f"Gemini #{i+1}: TEMP - {detail}")
            else: lines.append(f"Gemini #{i+1}: DEAD - {detail}")
        lines.append(f"Total: {alive} alive, {temp} temp, {len(g_keys)} total")
        
        rep = self.config.get("replicate_token", "")
        if rep:
            try:
                req = urllib.request.Request("https://api.replicate.com/v1/account", headers={"Authorization": f"Token {rep}"})
                with urllib.request.urlopen(req, timeout=10) as r: lines.append("Replicate: OK" if r.status == 200 else f"Replicate: {r.status}")
            except urllib.error.HTTPError as e:
                body = ""
                try: body = e.read().decode()[:200]
                except Exception: pass
                if e.code == 403 and "1010" in body: lines.append("Replicate: 403/1010 - billing needed")
                else: lines.append(f"Replicate: HTTP {e.code}")
            except Exception as e: lines.append(f"Replicate: error {str(e)[:40]}")
        else: lines.append("Replicate: no token")
        
        or_key = self.config.get("openrouter_key", "")
        if or_key:
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {or_key}"})
                with urllib.request.urlopen(req, timeout=10) as r: lines.append("OpenRouter: OK" if r.status == 200 else f"OpenRouter: {r.status}")
            except Exception as e: lines.append(f"OpenRouter: error {str(e)[:40]}")
        else: lines.append("OpenRouter: no key")
        
        hf = self.config.get("hf_token", "")
        if hf:
            try:
                req = urllib.request.Request("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {hf}"})
                with urllib.request.urlopen(req, timeout=10) as r: lines.append("HuggingFace: OK" if r.status == 200 else f"HuggingFace: {r.status}")
            except Exception as e: lines.append(f"HuggingFace: error {str(e)[:40]}")
        else: lines.append("HuggingFace: no token")
        
        fk = self.config.get("fish_key", "")
        if fk:
            try:
                payload = json.dumps({"text": "test", "format": "mp3"}).encode()
                req = urllib.request.Request("https://api.fish.audio/v1/tts", data=payload, headers={"Authorization": f"Bearer {fk}", "Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=20) as r:
                    d = r.read(); lines.append("Fish.audio: OK" if _looks_like_audio(d) else "Fish.audio: not audio")
            except urllib.error.HTTPError as e:
                if e.code == 402: lines.append("Fish.audio: 402 - no balance")
                elif e.code == 401: lines.append("Fish.audio: 401 - invalid")
                else: lines.append(f"Fish.audio: HTTP {e.code}")
            except Exception as e: lines.append(f"Fish.audio: error {str(e)[:40]}")
        else: lines.append("Fish.audio: no key")
        
        try: lines.append(f"Storage: {get_storage_root()}")
        except Exception: lines.append("Storage: error")
        Clock.schedule_once(lambda dt: self._show_diag_dialog("\n".join(lines)), 0)

    def _show_diag_dialog(self, text):
        try:
            if self.diag_dialog: self.diag_dialog.dismiss()
        except Exception: pass
        self.diag_dialog = MDDialog(title="Diagnostics", text=text, buttons=[MDRaisedButton(text="Close", on_release=lambda i: self.diag_dialog.dismiss())]); self.diag_dialog.open()

    def open_file_manager(self, purpose="voice"):
        self.file_manager_purpose = purpose
        if platform == "android":
            try:
                from jnius import autoclass
                Env = autoclass("android.os.Environment")
                if not Env.isExternalStorageManager(): toast("Need file access"); self._request_all_files_access()
            except Exception: pass
        start = "/storage/emulated/0" if platform == "android" else os.path.expanduser("~"); self.file_manager.show(start)

    def on_file_selected(self, path):
        self.exit_file_manager()
        if self.file_manager_purpose == "keys":
            if path.lower().endswith(".json"): self._import_keys_from_json(path)
            else: toast("Need .json!")
        elif path.lower().endswith((".mp3", ".wav", ".ogg", ".m4a")):
            self.current_voice_sample = path; self.modes["single"].ids.voice_sample_label.text = f"Voice: {os.path.basename(path)}"; toast("Sample attached!")
        else: toast("Need audio file!")

    def clear_voice_sample(self):
        self.current_voice_sample = None; self.modes["single"].ids.voice_sample_label.text = "Voice: none"

    def exit_file_manager(self, *args): self.file_manager.close()

    def show_onboarding(self):
        self.onboard_idx = 0; self._render_onboard_card()

    def _render_onboard_card(self):
        cards = [
            ("LEMUS AI MUSIC STUDIO", "Producer station with AI.\nModes: Single, Prompt, Hit, Album, Background.\nTrack: enhanced prompt -> plan -> sound -> cover -> master."),
            ("Sound Quality", "Studio sound: Replicate MusicGen (token + billing).\nFallback: HuggingFace -> Pollinations.\nLast resort: Local Arranger V4 (pads, bass, drums, melody, mastering). NOT a sine wave."),
            ("Gemini Models", "Models fetched from API automatically.\nAQ.* keys are temporary (~1h). Create permanent AIza* at aistudio.google.com/apikey."),
            ("Prompt Enhancer", "Write rough - studio finishes.\n'Olesya song about Kuper, soft drumm and base' ->\n'Melodic drum and bass, female vocals, warm pads, 174 bpm, sad mood.'"),
            ("Built-in Player", "Mini-player with cover and seek (yellow line).\nDownload button saves to Download."),
            ("Voice & Accents", "• Demo voice 10-30s -> Fish.audio clone\n• Accents via + (auto-clean)\n• Clone copies intonation"),
            ("Formats", "• WAV 44.1/16 - master\n• MP3 320 always\n• FLAC / m4a with ffmpeg\n• ZIP package"),
        ]
        card = cards[self.onboard_idx]; is_last = self.onboard_idx == len(cards) - 1
        def on_next(i):
            self.onboard_idx += 1
            if self.onboard_idx >= len(cards): self._close_onboarding()
            else: self._render_onboard_card()
        def on_prev(i):
            if self.onboard_idx > 0: self.onboard_idx -= 1; self._render_onboard_card()
        buttons = []
        if self.onboard_idx > 0: buttons.append(MDFlatButton(text="Back", on_release=on_prev))
        if is_last: buttons.append(MDRaisedButton(text="Start", on_release=lambda i: self._close_onboarding()))
        else: buttons.append(MDRaisedButton(text=f"Next ({self.onboard_idx+2}/{len(cards)})", on_release=on_next))
        try:
            if self.onboard_dialog: self.onboard_dialog.dismiss()
        except Exception: pass
        self.onboard_dialog = MDDialog(title=card[0], text=card[1], buttons=buttons); self.onboard_dialog.open()

    def _close_onboarding(self):
        try:
            if self.onboard_dialog: self.onboard_dialog.dismiss()
        except Exception: pass
        try:
            with open(os.path.join(self.get_data_path(), ONBOARDING_FLAG), "w") as f: f.write("1")
        except Exception: pass
        toast("Welcome!")

    def show_monetize_guide(self):
        self.monetize_idx = 0; self._render_monetize_card()

    def _render_monetize_card(self):
        cards = [
            ("Path to Payouts", "1. Distributor\n2. Files\n3. Registration\n4. Promotion\n5. Analytics"),
            ("Distributors", "RU: ONErpm (15%), Multiza (20%)\nWorld: DistroKid, TuneCore, CD Baby"),
            ("Files for Yandex", "• WAV 44.1/16/Stereo (or FLAC)\n• Cover 3000x3000 sRGB\n• MP3 320 kbps\n• -14 LUFS / TP -1.0"),
            ("AI Status", "Marking AI is your choice.\nIn RU no mandatory marking.\nFlag in passport and ZIP."),
            ("Revenue Calc", "Button 'Calculator' in library:\nstreams -> money by rates\nSpotify / Yandex / Apple."),
            ("Upload", "one-rpm.com -> WAV + cover -> release in 2 weeks -> moderation 1-3 days"),
            ("Economy", "• Spotify 1000 ≈ $3-5\n• Yandex 1000 ≈ 50-100 RUB\n• Apple 1000 ≈ $7\n• Background genres = listening hours"),
        ]
        title, body = cards[self.monetize_idx]; is_last = self.monetize_idx == len(cards) - 1
        def on_next(i):
            self.monetize_idx += 1
            if self.monetize_idx >= len(cards):
                try: self.monetize_dialog.dismiss()
                except Exception: pass
            else: self._render_monetize_card()
        def on_prev(i):
            if self.monetize_idx > 0: self.monetize_idx -= 1; self._render_monetize_card()
        buttons = []
        if self.monetize_idx > 0: buttons.append(MDFlatButton(text="Back", on_release=on_prev))
        if is_last: buttons.append(MDRaisedButton(text="Close", on_release=lambda i: self.monetize_dialog.dismiss()))
        else: buttons.append(MDRaisedButton(text=f"Next ({self.monetize_idx+2}/{len(cards)})", on_release=on_next))
        try:
            if self.monetize_dialog: self.monetize_dialog.dismiss()
        except Exception: pass
        self.monetize_dialog = MDDialog(title=title, text=body, buttons=buttons); self.monetize_dialog.open()

    def show_revenue_calculator(self):
        tf = MDTextField(hint_text="Streams", text="10000", mode="rectangle"); dlg = None
        def calc(inst):
            try: streams = int(tf.text.strip() or "0")
            except ValueError: toast("Enter number"); return
            spotify = streams / 1000 * 4.0; yandex = streams / 1000 * 75.0; apple = streams / 1000 * 7.0
            try: dlg.dismiss()
            except Exception: pass
            self._show_info("Revenue Forecast", f"Streams: {streams}\n\nSpotify: ${spotify:.2f}\nYandex: {yandex:.0f} RUB\nApple: ${apple:.2f}\n\nBackground genres x2-3 sessions.")
        dlg = MDDialog(title="Revenue Calculator", text="Rates: Spotify $4/1000, Yandex 75RUB/1000, Apple $7/1000.", content_cls=tf, buttons=[MDFlatButton(text="Cancel", on_release=lambda i: dlg.dismiss()), MDRaisedButton(text="Calculate", on_release=calc)]); dlg.open()

    def _show_info(self, title, text):
        d = MDDialog(title=title, text=text, buttons=[MDRaisedButton(text="Close", on_release=lambda i: d.dismiss())]); d.open()

    def add_to_history(self, ptype, text):
        self.history.append({"type": ptype, "prompt": text[:200], "date": time.strftime("%Y-%m-%d %H:%M")}); self.save_history()

    def show_prompt_history(self):
        if not self.history: toast("History empty"); return
        items = "\n\n".join([f"[{h['type']}] {h['date']}\n{h['prompt']}" for h in reversed(self.history[-8:])]); dlg = None
        def clear_h(i):
            self.history = []; self.save_history()
            try: dlg.dismiss()
            except Exception: pass
            toast("Cleared")
        def use_last(i):
            self.modes["single"].ids.s_title_input.text = self.history[-1]["prompt"]
            try: dlg.dismiss()
            except Exception: pass
            toast("Inserted")
        dlg = MDDialog(title="Prompt History", text=items[:1800], buttons=[MDFlatButton(text="Clear", on_release=clear_h), MDRaisedButton(text="Repeat Last", on_release=use_last)]); dlg.open()

    def show_crash_log(self):
        parts = []
        for name in ["startup_debug.log", "crash.log"]:
            for base in [".", get_storage_root()]:
                p = os.path.join(base, name)
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f: parts.append(f"--- {name} ---\n" + f.read()[-1500:])
                    except Exception: pass
        if not parts: toast("Logs empty"); return
        text = "\n\n".join(parts)[:3000]; d = MDDialog(title="Startup Logs", text=text, buttons=[MDRaisedButton(text="Close", on_release=lambda i: d.dismiss())]); d.open()

    def send_logs_to_me(self):
        import tempfile; chunks = []
        for name in ["startup_debug.log", "crash.log"]:
            for base in [".", get_storage_root()]:
                p = os.path.join(base, name)
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            data = f.read()[-3000:]
                        if data.strip(): chunks.append(f"===== {name} =====\n{data}")
                    except Exception: pass
        if not chunks: toast("Logs empty"); return
        body = "\n\n".join(chunks)[:6000]; tmp = os.path.join(tempfile.gettempdir(), "lemus_logs.txt")
        try:
            with open(tmp, "w", encoding="utf-8") as f: f.write(body)
        except Exception: toast("Log file error"); return
        if platform == "android":
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent"); Uri = autoclass("android.net.Uri"); File = autoclass("java.io.File"); PythonActivity = autoclass("org.kivy.android.PythonActivity")
                intent = Intent(Intent.ACTION_SEND); intent.setType("text/plain"); intent.putExtra(Intent.EXTRA_STREAM, Uri.fromFile(File(tmp))); intent.putExtra(Intent.EXTRA_SUBJECT, f"Lemus logs v{CURRENT_VERSION}")
                PythonActivity.mActivity.startActivity(Intent.createChooser(intent, "Send logs")); return
            except Exception as e: toast(f"Send err: {str(e)[:50]}")
        self._show_info("Logs", body[:1500])

    def backup_to_yandex(self):
        token = self.config.get("yandex_token", "")
        if not token: toast("Yandex token not set"); return
        if not self.projects: toast("No projects"); return
        toast("Backing up..."); threading.Thread(target=self._yandex_backup_thread, args=(token,)).start()

    def _yandex_backup_thread(self, token):
        base_dir = "/LemusStudio/"
        try:
            req = urllib.request.Request(f"https://cloud-api.yandex.net/v1/disk/resources?path={base_dir}", headers={"Authorization": f"OAuth {token}", "Content-Type": "application/json"}, method="PUT")
            try: urllib.request.urlopen(req, timeout=15)
            except Exception: pass
            uploaded = 0
            for item in self.projects[:5]:
                mp3 = item.get("mp3_path")
                if mp3 and os.path.exists(mp3):
                    fname = os.path.basename(mp3)
                    req2 = urllib.request.Request(f"https://cloud-api.yandex.net/v1/disk/resources/upload?path={base_dir}{fname}&overwrite=true", headers={"Authorization": f"OAuth {token}"})
                    with urllib.request.urlopen(req2, timeout=15) as r2:
                        d = json.loads(r2.read()); href = d.get("href")
                        if href:
                            with open(mp3, "rb") as f: data = f.read()
                            req3 = urllib.request.Request(href, data=data, method="PUT"); urllib.request.urlopen(req3, timeout=60); uploaded += 1
            Clock.schedule_once(lambda dt: toast(f"Uploaded: {uploaded}"), 0)
        except Exception as e: Clock.schedule_once(lambda dt: toast(f"Yandex err: {str(e)[:50]}"), 0)

    def _send_notification(self, title, text):
        if platform != "android": return
        try:
            from jnius import autoclass
            PythonActivity = autoclass("org.kivy.android.PythonActivity"); NotificationBuilder = autoclass("android.app.Notification$Builder"); Context = autoclass("android.content.Context")
            activity = PythonActivity.mActivity
            if activity:
                builder = NotificationBuilder(activity, "lemus_channel"); builder.setContentTitle(title); builder.setContentText(text); builder.setSmallIcon(activity.getApplicationInfo().icon); builder.setAutoCancel(True)
                nm = activity.getSystemService(Context.NOTIFICATION_SERVICE); nm.notify(int(time.time()) % 10000, builder.build())
        except Exception as e: print(f"Notif: {e}")

    def check_for_updates(self, silent=False): threading.Thread(target=self._check_update_thread, args=(silent,)).start()

    def _check_update_thread(self, silent):
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", headers={"User-Agent": "LemusStudio/7.6", "Accept": "application/vnd.github.v3+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode()); remote = data.get("tag_name", "").lstrip("v"); apk_url = None
                for a in data.get("assets", []):
                    if a.get("name", "").endswith(".apk"): apk_url = a.get("browser_download_url"); break
                if remote and self._is_newer(remote, CURRENT_VERSION) and apk_url:
                    Clock.schedule_once(lambda dt: self._show_update_dialog(remote, data.get("body", ""), apk_url), 0)
                elif not silent: Clock.schedule_once(lambda dt: toast(f"v{CURRENT_VERSION} actual"), 0)
        except Exception:
            if not silent: Clock.schedule_once(lambda dt: toast("Update server unavailable"), 0)

    def _is_newer(self, r, c):
        try: return [int(x) for x in r.split(".")] > [int(x) for x in c.split(".")]
        except Exception: return False

    def _show_update_dialog(self, v, cl, url):
        def confirm(i): self.update_dialog.dismiss(); self._download_apk(url)
        def cancel(i): self.update_dialog.dismiss()
        self.update_dialog = MDDialog(title=f"Update v{v}", text=f"Current: v{CURRENT_VERSION}\n\n{cl[:250]}", buttons=[MDFlatButton(text="Later", on_release=cancel), MDRaisedButton(text="Update", on_release=confirm)]); self.update_dialog.open()

    def _download_apk(self, url):
        toast("Downloading..."); threading.Thread(target=self._dl_worker, args=(url,)).start()

    def _dl_worker(self, url):
        dest = os.path.join(get_storage_root(), "update.apk")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LemusStudio"})
            with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f: f.write(r.read())
            Clock.schedule_once(lambda dt: self._install_apk(dest), 0)
        except Exception as e: Clock.schedule_once(lambda dt: toast(f"Download err: {str(e)[:50]}"), 0)

    def _install_apk(self, apk_path):
        if platform == "android":
            try:
                from jnius import autoclass
                PythonActivity = autoclass("org.kivy.android.PythonActivity"); Intent = autoclass("android.content.Intent"); Uri = autoclass("android.net.Uri"); File = autoclass("java.io.File")
                intent = Intent(Intent.ACTION_VIEW); intent.setDataAndType(Uri.fromFile(File(apk_path)), "application/vnd.android.package-archive"); intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_GRANT_READ_URI_PERMISSION)
                PythonActivity.mActivity.startActivity(intent)
            except Exception as e: toast(f"Install err: {str(e)[:50]}")
        else: toast(f"File: {apk_path}")

if __name__ == "__main__":
    _log("=== ENTRY POINT ===")
    try:
        LemusStudioApp().run()
    except Exception as e:
        _log(f"FATAL: {e}")
        traceback.print_exc()
        try:
            with open("crash_fatal.log", "w", encoding="utf-8") as f:
                f.write(f"Crash {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
