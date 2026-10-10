"""乐器配置档案：让判定逻辑跟着"这把琴 + 这支麦克风"走，而不是写死。

## 为什么需要它

2026-10-07 那版判定逻辑里有一批"固定纠正"：低频存在性规则、固定的
``low_max_hz=420``、固定的门限与容差。这些值是为**当时那支低频响应极差的
内置麦克风**调的。2026-10-10 用户换上正常麦克风、并把琴调准之后重新录音，
同一套规则反而**把正确的读数改错**（第 1 弦 E4 329Hz → E3 164.6Hz，误差 1200 音分）。

单帧频谱无法可靠区分"真锁定"与"琴箱共振/房间低频"，所以正确做法不是再猜一个
更好的阈值，而是：

1. **把这类会随设备变化的策略变成配置**，由实测决定，而不是写死在代码里；
2. **用配置里的预期音高集消解歧义**（调音时知道该弹哪根弦、练习时知道目标音，
   根本不需要靠频谱猜八度）；
3. **发现声音与配置不符时，明确提示用户**，而不是默默按某个猜测处理。

## 档案内容

- 每根弦：标准音高、实测频率、音分偏差、电平
- 检测策略：谐波校验模式、频率范围、置信度下限、低频增强开关
- 调音基准：A4 参考频率、整体偏差
- 备注：测量时间与条件

## 用法

    profile = standard_profile()                    # 内置默认（标准调弦）
    profile = load_profile(path)                    # 从 JSON 读
    save_profile(profile, path)                     # 写回
    profile = profile_from_measurements(measured)   # 由实测生成

    # 判定时：把检测频率对到本琴可弹的音上
    resolution = resolve_hz(329.14, profile)
    resolution.midi, resolution.status              # 64, "exact"

    # 校验：哪些地方与配置不符，需要提示用户
    warnings = check_measurements(measured, profile)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .theory.notes import cents_between, hz_to_midi, midi_name, midi_to_hz
from .audio.pitch_yin import HARMONIC_MODES

#: 文件格式标识与版本
PROFILE_FORMAT = "jitatrainer-instrument"
PROFILE_VERSION = 1

#: 标准调弦（弦号 → MIDI）：6→1 为 E2 A2 D3 G3 B3 E4
STANDARD_TUNING: tuple[tuple[int, int], ...] = ((6, 40), (5, 45), (4, 50), (3, 55), (2, 59), (1, 64))

#: 常见调弦预设（供用户直接选，不必自己填）
TUNING_PRESETS: dict[str, tuple[tuple[int, int], ...]] = {
    "standard": STANDARD_TUNING,
    "drop_d": ((6, 38), (5, 45), (4, 50), (3, 55), (2, 59), (1, 64)),
    "half_step_down": ((6, 39), (5, 44), (4, 49), (3, 54), (2, 58), (1, 63)),
    "full_step_down": ((6, 38), (5, 43), (4, 48), (3, 53), (2, 57), (1, 62)),
    "open_g": ((6, 38), (5, 43), (4, 50), (3, 55), (2, 59), (1, 62)),
    "dadgad": ((6, 38), (5, 45), (4, 50), (3, 55), (2, 57), (1, 62)),
}

TUNING_PRESET_LABELS: dict[str, str] = {
    "standard": "标准调弦 EADGBE",
    "drop_d": "Drop D（6 弦降全音）",
    "half_step_down": "降半音 Eb",
    "full_step_down": "降全音 D",
    "open_g": "Open G",
    "dadgad": "DADGAD",
}

#: 判定"这把弦调准了"的容差（音分）
IN_TUNE_CENTS = 12.0
#: 超过这个偏差就建议用户调音（音分）
TUNING_WARN_CENTS = 20.0
#: 某根弦比其他弦低这么多的电平就提示麦克风摆位（dB）
LEVEL_IMBALANCE_DB = 12.0
#: 检测频率与"本琴可弹音"的最大容许偏差（音分）；超过说明检测结果解释不通
UNEXPLAINED_CENTS = 60.0


@dataclass(frozen=True, slots=True)
class StringSpec:
    """一根弦。"""

    number: int
    midi: int
    #: 实测基频（Hz）。未测量时为 None
    measured_hz: float | None = None
    #: 实测电平（dBFS 或相对值），用于发现摆位/响应问题
    level_db: float | None = None
    #: 实测置信度
    confidence: float | None = None

    @property
    def note_name(self) -> str:
        return midi_name(self.midi)

    @property
    def standard_hz(self) -> float:
        return midi_to_hz(self.midi)

    @property
    def offset_cents(self) -> float | None:
        """实测相对标准音高的音分偏差。"""
        if not self.measured_hz:
            return None
        return cents_between(self.measured_hz, self.standard_hz)

    @property
    def tuning_cents(self) -> float | None:
        """相对**本档案预期音高**的偏差。

        档案里 ``midi`` 是该弦在这把琴上的目标音（未必是标准调弦），
        因此这个值才是"有没有调准"的判据。
        """
        return self.offset_cents

    def to_json(self) -> dict:
        return {
            "number": self.number,
            "midi": self.midi,
            "note": self.note_name,
            "standard_hz": round(self.standard_hz, 2),
            "measured_hz": round(self.measured_hz, 2) if self.measured_hz else None,
            "offset_cents": round(self.offset_cents, 1) if self.offset_cents is not None else None,
            "level_db": round(self.level_db, 1) if self.level_db is not None else None,
            "confidence": round(self.confidence, 3) if self.confidence is not None else None,
        }

    @staticmethod
    def from_json(data: dict) -> StringSpec:
        return StringSpec(
            number=int(data["number"]),
            midi=int(data["midi"]),
            measured_hz=data.get("measured_hz"),
            level_db=data.get("level_db"),
            confidence=data.get("confidence"),
        )


@dataclass(frozen=True, slots=True)
class DetectionSettings:
    """随"琴 + 麦克风"变化的检测策略。"""

    #: off / auto / presence，见 pitch_yin.harmonic_correct
    harmonic_mode: str = "auto"
    fmin: float = 70.0
    fmax: float = 1400.0
    confidence_min: float = 0.80
    low_enhance: bool = True
    low_band_hz: float = 220.0
    #: 判定音准的容差（音分），会传给调音器显示
    tolerance_cents: float = 25.0

    def validate(self) -> None:
        if self.harmonic_mode not in HARMONIC_MODES:
            raise ValueError(f"未知谐波校验模式：{self.harmonic_mode}")
        if self.fmin <= 0 or self.fmax <= self.fmin:
            raise ValueError("频率范围不合法")

    def to_json(self) -> dict:
        return {
            "harmonic_mode": self.harmonic_mode,
            "fmin": self.fmin,
            "fmax": self.fmax,
            "confidence_min": self.confidence_min,
            "low_enhance": self.low_enhance,
            "low_band_hz": self.low_band_hz,
            "tolerance_cents": self.tolerance_cents,
        }

    @staticmethod
    def from_json(data: dict) -> DetectionSettings:
        base = DetectionSettings()
        return DetectionSettings(
            harmonic_mode=str(data.get("harmonic_mode", base.harmonic_mode)),
            fmin=float(data.get("fmin", base.fmin)),
            fmax=float(data.get("fmax", base.fmax)),
            confidence_min=float(data.get("confidence_min", base.confidence_min)),
            low_enhance=bool(data.get("low_enhance", base.low_enhance)),
            low_band_hz=float(data.get("low_band_hz", base.low_band_hz)),
            tolerance_cents=float(data.get("tolerance_cents", base.tolerance_cents)),
        )


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    """一把琴 + 一套录音环境的配置档案。"""

    id: str
    name: str
    strings: tuple[StringSpec, ...]
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    #: A4 参考频率（多数情况 440）
    reference_a4_hz: float = 440.0
    #: 这把琴最高到第几品（决定"可弹音集"的范围）
    max_fret: int = 12
    created_at: str = ""
    notes: str = ""
    source: str = "builtin"  # builtin | measured | imported

    # ------------------------------------------------------------------ 基本
    def validate(self) -> None:
        if not self.strings:
            raise ValueError("配置档案至少要有一根弦")
        numbers = [spec.number for spec in self.strings]
        if len(set(numbers)) != len(numbers):
            raise ValueError("弦号重复")
        self.detection.validate()

    @property
    def is_measured(self) -> bool:
        return any(spec.measured_hz for spec in self.strings)

    @property
    def string_numbers(self) -> tuple[int, ...]:
        return tuple(spec.number for spec in self.strings)

    def string(self, number: int) -> StringSpec | None:
        for spec in self.strings:
            if spec.number == number:
                return spec
        return None

    def open_midis(self) -> tuple[int, ...]:
        return tuple(spec.midi for spec in self.strings)

    def playable_midis(self, *, max_fret: int | None = None) -> tuple[int, ...]:
        """本琴可弹的全部音高（各弦 0 品到最高品，去重排序）。"""
        fret_limit = self.max_fret if max_fret is None else max_fret
        pitches = {
            spec.midi + fret
            for spec in self.strings
            for fret in range(0, max(0, fret_limit) + 1)
        }
        return tuple(sorted(pitches))

    # ------------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        return {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "source": self.source,
            "reference_a4_hz": self.reference_a4_hz,
            "max_fret": self.max_fret,
            "notes": self.notes,
            "detection": self.detection.to_json(),
            "strings": [spec.to_json() for spec in self.strings],
        }

    @staticmethod
    def from_json(data: dict) -> InstrumentProfile:
        if data.get("format") != PROFILE_FORMAT:
            raise ProfileError("不是 JitaTrainer 乐器配置档案")
        version = data.get("version")
        if not isinstance(version, int) or version > PROFILE_VERSION:
            raise ProfileError(f"配置档案版本 {version} 高于本程序支持的 {PROFILE_VERSION}")
        profile = InstrumentProfile(
            id=str(data.get("id") or "custom"),
            name=str(data.get("name") or "自定义"),
            strings=tuple(StringSpec.from_json(item) for item in data.get("strings") or []),
            detection=DetectionSettings.from_json(data.get("detection") or {}),
            reference_a4_hz=float(data.get("reference_a4_hz", 440.0)),
            max_fret=int(data.get("max_fret", 12)),
            created_at=str(data.get("created_at") or ""),
            notes=str(data.get("notes") or ""),
            source=str(data.get("source") or "imported"),
        )
        profile.validate()
        return profile


class ProfileError(ValueError):
    """配置档案格式或内容有问题。"""


class StringMeasurer:
    """在界面上逐根弦测量时的累计器（纯逻辑，不依赖音频与界面）。

    界面每收到一帧检测结果就 ``add()``，一小段采集结束后取 ``result()``：
    用**中位频率**而不是平均，避免个别跳变的帧把结果拉偏。
    """

    def __init__(self, number: int, target_midi: int) -> None:
        self.number = number
        self.target_midi = target_midi
        self._pitches: list[float] = []
        self._levels: list[float] = []
        self._confidences: list[float] = []

    def add(
        self, hz: float, *, level_db: float | None = None, confidence: float | None = None
    ) -> None:
        if hz is None or hz <= 0:
            return
        self._pitches.append(float(hz))
        if level_db is not None:
            self._levels.append(float(level_db))
        if confidence is not None:
            self._confidences.append(float(confidence))

    @property
    def frames(self) -> int:
        return len(self._pitches)

    @property
    def target_hz(self) -> float:
        return midi_to_hz(self.target_midi)

    @property
    def median_hz(self) -> float | None:
        if not self._pitches:
            return None
        ordered = sorted(self._pitches)
        return ordered[len(ordered) // 2]

    @property
    def cents(self) -> float | None:
        hz = self.median_hz
        return None if hz is None else cents_between(hz, self.target_hz)

    def result(self, *, min_frames: int = 3) -> StringMeasurement | None:
        """样本太少时返回 None（说明这根弦没弹响或没测到）。"""
        if self.frames < min_frames:
            return None
        levels = sorted(self._levels)
        confidences = sorted(self._confidences)
        return StringMeasurement(
            number=self.number,
            hz=float(self.median_hz or 0.0),
            level_db=levels[len(levels) // 2] if levels else None,
            confidence=confidences[len(confidences) // 2] if confidences else None,
            expected_midi=self.target_midi,
        )

    def verdict(self, *, in_tune_cents: float = IN_TUNE_CENTS) -> str:
        """给界面看的一句话结论。"""
        cents = self.cents
        if cents is None:
            return "未测到"
        if abs(cents) <= in_tune_cents:
            return "准"
        return f"{'偏高' if cents > 0 else '偏低'} {abs(cents):.0f} 音分"


# ---------------------------------------------------------------------------
# 内置档案
# ---------------------------------------------------------------------------
def standard_profile(*, max_fret: int = 12) -> InstrumentProfile:
    """内置默认档案：标准调弦六弦吉他，检测策略取保守默认值。"""
    return InstrumentProfile(
        id="standard-6-eadgbe",
        name="标准六弦吉他（EADGBE）",
        strings=tuple(
            StringSpec(number=number, midi=midi) for number, midi in STANDARD_TUNING
        ),
        detection=DetectionSettings(),
        max_fret=max_fret,
        source="builtin",
        notes="内置默认。检测策略保守（不做低频频谱纠正）；如需按你的琴与麦克风调整，请运行测量。",
    )


def profile_from_tuning(
    preset: str,
    *,
    name: str | None = None,
    detection: DetectionSettings | None = None,
    max_fret: int = 12,
) -> InstrumentProfile:
    """按调弦预设建一个档案（用户没做测量时的快速选择）。"""
    tuning = TUNING_PRESETS.get(preset)
    if tuning is None:
        raise ProfileError(f"未知调弦预设：{preset}")
    return InstrumentProfile(
        id=f"tuning-{preset}",
        name=name or TUNING_PRESET_LABELS.get(preset, preset),
        strings=tuple(StringSpec(number=number, midi=midi) for number, midi in tuning),
        detection=detection or DetectionSettings(),
        max_fret=max_fret,
        source="builtin",
        notes=f"按调弦预设创建：{preset}（未做实测）。",
    )


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_profile(path: Path | str) -> InstrumentProfile:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"不是合法的 JSON：{exc}") from exc
    except OSError as exc:
        raise ProfileError(f"无法读取配置档案：{exc}") from exc
    return InstrumentProfile.from_json(data)


def save_profile(profile: InstrumentProfile, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(profile.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def list_profiles(directory: Path | str) -> list[Path]:
    folder = Path(directory)
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"))


def profiles_directory() -> Path:
    """乐器档案的默认目录（``data/instruments``）。"""
    from ..paths import data_dir

    return data_dir() / "instruments"


def list_saved_profiles() -> list[tuple[Path, InstrumentProfile]]:
    """列出已保存的乐器档案。坏文件跳过而不是让整个列表失败。"""
    result: list[tuple[Path, InstrumentProfile]] = []
    for path in list_profiles(profiles_directory()):
        try:
            result.append((path, load_profile(path)))
        except ProfileError:
            continue
    result.sort(key=lambda item: item[1].name)
    return result


def rename_saved_profile(path: Path | str, new_name: str) -> Path:
    """改名（同时改 id 与文件名，避免名字与文件对不上）。"""
    source = Path(path)
    profile = load_profile(source)
    cleaned = new_name.strip()
    if not cleaned:
        raise ProfileError("名称不能为空")
    renamed = replace(profile, name=cleaned)
    target = source.parent / f"{_safe_filename(cleaned)}.json"
    save_profile(renamed, target)
    if target != source and source.exists():
        source.unlink()
    return target


def delete_saved_profile(path: Path | str) -> bool:
    """删除档案文件。文件不存在返回 False。"""
    target = Path(path)
    if not target.is_file():
        return False
    target.unlink()
    return True


def _safe_filename(name: str) -> str:
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()
    return safe or "profile"


# ---------------------------------------------------------------------------
# 由实测生成档案
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StringMeasurement:
    """对某根弦的一次实测结果。"""

    number: int
    hz: float
    level_db: float | None = None
    confidence: float | None = None
    #: 被测量时该弦的目标音高（通常取标准调弦）
    expected_midi: int | None = None

    @property
    def midi(self) -> float:
        return hz_to_midi(self.hz)


def profile_from_measurements(
    measurements: Sequence[StringMeasurement],
    *,
    name: str,
    tuning: Sequence[tuple[int, int]] = STANDARD_TUNING,
    detection: DetectionSettings | None = None,
    max_fret: int = 12,
    notes: str = "",
    profile_id: str | None = None,
) -> InstrumentProfile:
    """由实测结果生成档案。

    目标音高按 ``tuning`` 给出的标准值记录（**不**把测量偏差写进目标，
    否则琴没调准会被当成"新调弦"固化下来）；偏差单独记录在 ``offset_cents``，
    用来提示用户调音。
    """
    targets = dict(tuning)
    by_number = {item.number: item for item in measurements}
    strings: list[StringSpec] = []
    for number, midi in tuning:
        measured = by_number.get(number)
        strings.append(
            StringSpec(
                number=number,
                midi=midi,
                measured_hz=measured.hz if measured else None,
                level_db=measured.level_db if measured else None,
                confidence=measured.confidence if measured else None,
            )
        )
    del targets
    return InstrumentProfile(
        id=profile_id or f"measured-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        name=name,
        strings=tuple(strings),
        detection=detection or DetectionSettings(),
        max_fret=max_fret,
        created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        notes=notes or "由实测生成。偏差已记录在 offset_cents，仅作提示，不影响目标音高。",
        source="measured",
    )


# ---------------------------------------------------------------------------
# 频率 → 本琴的音
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Resolution:
    """把检测频率解释成"本琴上的某个音"。"""

    hz: float
    midi: int
    cents: float
    status: str  # exact | off_tune | octave_adjusted | unexplained
    adjusted_from: float | None = None
    alternatives: tuple[tuple[int, float], ...] = ()

    @property
    def note_name(self) -> str:
        return midi_name(self.midi)

    @property
    def ok(self) -> bool:
        return self.status != "unexplained"


def _nearest_playable(hz: float, pitches: Sequence[int]) -> tuple[int, float]:
    """最接近 hz 的可弹音及其音分偏差。"""
    best_midi = pitches[0]
    best_cents = float("inf")
    for midi in pitches:
        cents = abs(cents_between(hz, midi_to_hz(midi)))
        if cents < best_cents:
            best_midi, best_cents = midi, cents
    return best_midi, cents


def resolve_hz(
    hz: float,
    profile: InstrumentProfile,
    *,
    tolerance_cents: float | None = None,
    expect_midi: int | None = None,
    try_harmonics: bool = True,
) -> Resolution:
    """把检测频率对到本琴可弹的音上，必要时消解八度/十二度歧义。

    优先级（**关键：先相信原读数**）：

    1. 给了 ``expect_midi``（调音器知道该弹哪根弦、练习知道目标音）→
       在该音的八度族里直接选最接近的，**不需要靠频谱猜**；
    2. 否则看原读数能否对上本琴的某个可弹音（在容差内）→ 直接采用；
    3. 原读数解释不通时，才尝试谐波关系（÷2、×2、÷3、×3），
       且只接受落在容差内的解释；
    4. 都不成立 → ``unexplained``，交给调用方**提示用户**。

    第 2 步的顺序很重要：早期实现直接在"原读数 + 各种谐波"里挑偏差最小的，
    结果会把一个本来正确的读数（164.6Hz，偏差 12 音分）通过 ×3 解释成完全
    不相干的 B4（偏差 0.6 音分）——**为了让数字好看而把音改错**。
    """
    if hz <= 0:
        return Resolution(hz=hz, midi=0, cents=0.0, status="unexplained")

    limit = tolerance_cents if tolerance_cents is not None else UNEXPLAINED_CENTS
    pitches = profile.playable_midis()

    # ---- 1. 已知预期音：判断读数与它的关系（调音器场景）----
    if expect_midi is not None:
        target_hz = midi_to_hz(expect_midi)
        direct = cents_between(hz, target_hz)
        if abs(direct) <= limit:
            status = (
                "exact" if abs(direct) <= profile.detection.tolerance_cents else "off_tune"
            )
            return Resolution(hz=hz, midi=expect_midi, cents=round(direct, 1), status=status)
        # 读数与预期音对不上：只考虑"检测值偏高"的情况。
        #
        # YIN 的已知失效模式是**锁到高次谐波**（把 E2 读成 E3/D#4），也就是检测值
        # 高于真实基频。因此只尝试把读数除以 2、除以 3。
        #
        # 反过来（检测值只有预期音的一半/三分之一）**不应当**被"纠正"成预期音：
        # 那更像用户弹了另一根弦（例如在调第 1 弦时弹响了第 5 弦 A2，330÷3 恰好
        # 落在 E4 附近），硬算成"准的"就是在骗用户。
        for factor in (0.5, 1 / 3):
            candidate = hz * factor
            cents = cents_between(candidate, target_hz)
            if abs(cents) <= limit:
                return Resolution(
                    hz=candidate,
                    midi=expect_midi,
                    cents=round(cents, 1),
                    status="octave_adjusted",
                    adjusted_from=hz,
                )
        # 与该弦完全对不上（可能弹的是别的弦，或环境噪声）
        return Resolution(hz=hz, midi=expect_midi, cents=round(direct, 1), status="unexplained")

    # ---- 2. 先看原读数 ----
    midi, _abs_cents = _nearest_playable(hz, pitches)
    signed = cents_between(hz, midi_to_hz(midi))
    if abs(signed) <= limit:
        status = "exact" if abs(signed) <= profile.detection.tolerance_cents else "off_tune"
        return Resolution(hz=hz, midi=midi, cents=round(signed, 1), status=status)

    # ---- 3. 原读数解释不通，再试谐波关系 ----
    alternatives: list[tuple[int, float]] = []
    if try_harmonics:
        for factor in (0.5, 2.0, 1 / 3, 3.0):
            candidate = hz * factor
            if candidate <= 0:
                continue
            other_midi, _ = _nearest_playable(candidate, pitches)
            other_cents = cents_between(candidate, midi_to_hz(other_midi))
            if abs(other_cents) <= limit:
                return Resolution(
                    hz=candidate,
                    midi=other_midi,
                    cents=round(other_cents, 1),
                    status="octave_adjusted",
                    adjusted_from=hz,
                )
            alternatives.append((other_midi, round(other_cents, 1)))

    # ---- 4. 解释不通 ----
    return Resolution(
        hz=hz,
        midi=midi,
        cents=round(signed, 1),
        status="unexplained",
        alternatives=tuple(alternatives[:3]),
    )


# ---------------------------------------------------------------------------
# 与配置不符时的提示
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProfileWarning:
    """一条给用户看的提示。"""

    code: str
    severity: str  # info | warn | error
    message: str
    advice: str = ""
    string_number: int | None = None

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "advice": self.advice,
            "string": self.string_number,
        }


def check_measurements(
    measurements: Sequence[StringMeasurement],
    profile: InstrumentProfile,
    *,
    tuning_warn_cents: float = TUNING_WARN_CENTS,
    level_imbalance_db: float = LEVEL_IMBALANCE_DB,
) -> list[ProfileWarning]:
    """把实测结果与配置比对，给出**可执行**的提示。

    这是需求里"发现声音不符合预期时提示用户处理"的具体实现：
    不猜测、不静默纠正，而是告诉用户哪里不对、该怎么办。
    """
    warnings: list[ProfileWarning] = []
    by_number = {item.number: item for item in measurements}

    # 1. 漏测的弦
    missing = [spec.number for spec in profile.strings if spec.number not in by_number]
    if missing:
        warnings.append(
            ProfileWarning(
                code="missing_strings",
                severity="warn",
                message=f"这些弦没有测到声音：{'、'.join(f'第{n}弦' for n in sorted(missing, reverse=True))}",
                advice="确认弦有拨响、麦克风没被挡住，然后重新测量。",
            )
        )

    # 2. 音准偏差
    off_tune: list[tuple[int, float]] = []
    for spec in profile.strings:
        measured = by_number.get(spec.number)
        if measured is None:
            continue
        cents = cents_between(measured.hz, midi_to_hz(spec.midi))
        if abs(cents) >= tuning_warn_cents:
            off_tune.append((spec.number, cents))
    for number, cents in off_tune:
        direction = "偏低" if cents < 0 else "偏高"
        warnings.append(
            ProfileWarning(
                code="out_of_tune",
                severity="warn",
                message=f"第{number}弦{direction} {abs(cents):.0f} 音分",
                advice="建议先调音再练习；判定容差之外会频繁误判。",
                string_number=number,
            )
        )

    # 3. 电平不均衡（麦克风摆位问题）
    levels = [
        (spec.number, by_number[spec.number].level_db)
        for spec in profile.strings
        if spec.number in by_number and by_number[spec.number].level_db is not None
    ]
    if len(levels) >= 3:
        values = [value for _n, value in levels if value is not None]
        strongest = max(values)
        quietest_number, quietest = min(levels, key=lambda item: item[1] if item[1] is not None else 0.0)
        if quietest is not None and strongest - quietest > level_imbalance_db:
            warnings.append(
                ProfileWarning(
                    code="level_imbalance",
                    severity="warn",
                    message=f"第{quietest_number}弦比其他弦低 {strongest - quietest:.0f} dB",
                    advice="低音弦对麦克风摆位很敏感，试着把麦克风靠近音孔或换一个位置。",
                    string_number=quietest_number,
                )
            )

    # 4. 整体偏差很大 → 可能换调弦了
    offsets = [
        cents_between(by_number[spec.number].hz, midi_to_hz(spec.midi))
        for spec in profile.strings
        if spec.number in by_number
    ]
    if offsets and all(abs(value) >= 60 for value in offsets) and len(offsets) >= 3:
        average = sum(offsets) / len(offsets)
        warnings.append(
            ProfileWarning(
                code="different_tuning",
                severity="error",
                message=f"整把琴平均偏差 {average:+.0f} 音分，看起来不是当前配置的调弦",
                advice="可能换了调弦或整体移调。请新建一个对应调弦的配置档案，或重新测量。",
            )
        )

    return warnings


def check_detection(
    hz: float,
    profile: InstrumentProfile,
) -> ProfileWarning | None:
    """单次检测结果与配置不符时的提示（实时路径用）。"""
    resolution = resolve_hz(hz, profile)
    if resolution.ok:
        return None
    return ProfileWarning(
        code="unexplained_pitch",
        severity="warn",
        message=f"检测到 {hz:.1f}Hz，不属于本琴可弹的音（最接近 {resolution.note_name}，"
        f"仍差 {abs(resolution.cents):.0f} 音分）",
        advice="可能是环境噪声、其他乐器，或麦克风摆位/增益需要调整。",
    )


def detection_settings_from_profile(
    profile: InstrumentProfile,
    *,
    samplerate: int = 48000,
):
    """把档案里的检测策略转成 ``AnalyzerConfig``（避免此处反向依赖音频层）。"""
    from .audio.analyzer import AnalyzerConfig

    return AnalyzerConfig(
        samplerate=samplerate,
        low_enhance=profile.detection.low_enhance,
        low_band_hz=profile.detection.low_band_hz,
        harmonic_mode=profile.detection.harmonic_mode,
    )


def yin_config_from_profile(profile: InstrumentProfile, *, samplerate: int = 48000):
    """把档案里的检测策略转成 ``YinConfig``。"""
    from .audio.pitch_yin import YinConfig

    return YinConfig(
        samplerate=samplerate,
        fmin=profile.detection.fmin,
        fmax=profile.detection.fmax,
        confidence_min=profile.detection.confidence_min,
        harmonic_mode=profile.detection.harmonic_mode,
    )


def tuning_from_profile(profile: InstrumentProfile):
    """由配置档案构造调音器用的 ``Tuning``。

    这样调音器的目标音高来自**你这把琴的档案**，而不是写死的标准调弦：
    换了降半音、Drop D 或者别的调弦，调音器跟着变，不需要改代码。
    """
    from .theory.tuning import Tuning

    ordered = sorted(profile.strings, key=lambda spec: -spec.number)
    return Tuning(
        id=profile.id,
        name_zh=profile.name,
        name_en=profile.name,
        open_strings=tuple(spec.midi for spec in ordered),
    )


def summarise(profile: InstrumentProfile) -> str:
    """一行摘要（界面与日志用）。"""
    if not profile.is_measured:
        return f"{profile.name}（未实测，{len(profile.strings)} 弦）"
    parts = []
    for spec in profile.strings:
        offset = spec.offset_cents
        if offset is None:
            continue
        parts.append(f"{spec.number}弦{offset:+.0f}")
    return f"{profile.name}（音分偏差：{' '.join(parts)}）"


__all__ = [
    "PROFILE_FORMAT",
    "PROFILE_VERSION",
    "STANDARD_TUNING",
    "TUNING_PRESETS",
    "TUNING_PRESET_LABELS",
    "IN_TUNE_CENTS",
    "TUNING_WARN_CENTS",
    "DetectionSettings",
    "InstrumentProfile",
    "ProfileError",
    "ProfileWarning",
    "Resolution",
    "StringMeasurer",
    "StringMeasurement",
    "StringSpec",
    "check_detection",
    "check_measurements",
    "delete_saved_profile",
    "detection_settings_from_profile",
    "list_profiles",
    "list_saved_profiles",
    "load_profile",
    "profile_from_measurements",
    "profile_from_tuning",
    "profiles_directory",
    "rename_saved_profile",
    "resolve_hz",
    "save_profile",
    "standard_profile",
    "summarise",
    "tuning_from_profile",
    "yin_config_from_profile",
]
