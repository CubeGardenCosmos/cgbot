"""cgscore 单元测试:合成 XML 与 MIDI 夹具。"""

from __future__ import annotations

import json
from pathlib import Path

import mido
import pytest

from cgscore.defaults import default_mix, family_for, muse_name_for
from cgscore.midisplit import group_to_midi, split_midi
from cgscore.musicxml import ScoreError, parse_score, part_to_midi
from cgscore.prepare import prepare

SIMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1">
      <part-name>Flute</part-name>
      <midi-instrument id="P1-I1">
        <midi-channel>1</midi-channel>
        <midi-program>74</midi-program>
      </midi-instrument>
    </score-part>
    <score-part id="P2">
      <part-name>Snare Drum</part-name>
      <score-instrument id="P2-X1"><midi-unpitched>39</midi-unpitched></score-instrument>
      <midi-instrument id="P2-X1">
        <midi-channel>10</midi-channel>
        <midi-unpitched>39</midi-unpitched>
      </midi-instrument>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <direction>
        <direction-type><words>pizz.</words></direction-type>
        <sound tempo="120"/>
      </direction>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>4</duration>
        <voice>1</voice>
        <type>quarter</type>
      </note>
      <direction><direction-type><words>arco</words></direction-type></direction>
      <note>
        <pitch><step>D</step><octave>4</octave></pitch>
        <duration>4</duration>
        <voice>1</voice>
        <type>quarter</type>
      </note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note>
        <unpitched><display-step>E</display-step><display-octave>4</display-octave></unpitched>
        <instrument id="P2-X1"/>
        <duration>4</duration>
        <type>quarter</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""

REPEAT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Solo</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions></attributes>
      <barline location="left"><repeat direction="forward"/></barline>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
      <barline location="right"><repeat direction="backward" times="2"/></barline>
    </measure>
  </part>
</score-partwise>
"""


def test_musicxml_parse_basic():
    parts, tempos, sigs = parse_score(SIMPLE_XML.encode("utf-8"), "simple.musicxml")
    assert len(parts) == 2
    assert parts[0].name == "Flute"
    assert not parts[0].drum
    assert parts[0].program == 73  # 0 基(midi-program 74 - 1)
    assert len(parts[0].notes) == 2
    # 拨奏切换:pizz -> arco
    pizz_events = parts[0].pizz_events
    assert len(pizz_events) == 2
    assert pizz_events[0].pizz is True
    assert pizz_events[1].pizz is False
    # 打击乐
    assert parts[1].drum is True
    assert parts[1].notes[0].pitch == 38  # midi-unpitched 39 - 1 = 38 (Snare)
    # 速度与拍号
    assert tempos[0].micros == 500_000
    assert sigs[0].beats == 4


def test_repeat_expansion():
    parts, _t, _s = parse_score(REPEAT_XML.encode("utf-8"), "repeat.xml")
    # 反复 2 次 -> 2 个音符
    assert len(parts[0].notes) == 2
    assert parts[0].notes[0].start == 0
    assert parts[0].notes[1].start == 480


def test_ties_merge():
    xml = """<?xml version="1.0"?>
    <score-partwise version="4.0">
      <part-list><score-part id="P1"><part-name>P</part-name></score-part></part-list>
      <part id="P1">
        <measure number="1">
          <attributes><divisions>1</divisions></attributes>
          <note>
            <pitch><step>C</step><octave>4</octave></pitch>
            <duration>1</duration>
            <tie type="start"/>
          </note>
          <note>
            <pitch><step>C</step><octave>4</octave></pitch>
            <duration>1</duration>
            <tie type="stop"/>
          </note>
        </measure>
      </part>
    </score-partwise>"""
    parts, _, _ = parse_score(xml.encode("utf-8"), "tie.xml")
    assert len(parts[0].notes) == 1
    assert parts[0].notes[0].start == 0
    assert parts[0].notes[0].end == 960


def test_part_to_midi_pizz_programs():
    parts, tempos, sigs = parse_score(SIMPLE_XML.encode("utf-8"), "simple.musicxml")
    mid = part_to_midi(parts[0], channel=0, tempos=tempos, sigs=sigs)
    pcs = [m.program for tr in mid.tracks for m in tr if m.type == "program_change"]
    # 初始 73, pizz 段 45, arco 段 73
    assert pcs == [73, 45, 73]


def test_defaults_mapping():
    assert family_for(0, False) == "piano"
    assert family_for(73, False) == "woodwinds"
    assert family_for(56, False) == "brass"
    assert family_for(0, True) == "drums"

    # Muse 乐器名
    assert muse_name_for("Flute 1", 73, False, 0) == "Flute 1"
    assert muse_name_for("Flute", 73, False, 1) == "Flute 2"
    assert muse_name_for("Violin I", 40, False, 0) == "Violins 1"
    assert muse_name_for("Violin II", 40, False, 1) == "Violins 2"
    assert muse_name_for("Accordion", 21, False, 0) == ""  # 强制 FluidSynth

    # 折入 -12 dB 规则
    _name, mix_flute = default_mix("Flute", 73, False)
    # woodwind balance -12 dB; Muse 有音色 -> 不削 12
    assert mix_flute["gain_db"] == -12.0

    _name, mix_drum = default_mix("Drum Kit", 0, True)
    # drum: OtherDrum -6 dB, 打击乐再削 12 dB -> -18 dB
    assert mix_drum["gain_db"] == -18.0


def test_split_midi_piano_two_hands(tmp_path: Path):
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    # 导轨
    c_tr = mido.MidiTrack()
    c_tr.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    mid.tracks.append(c_tr)
    # 右手
    r_tr = mido.MidiTrack()
    r_tr.append(mido.MetaMessage("track_name", name="Piano RH", time=0))
    r_tr.append(mido.Message("program_change", channel=0, program=0, time=0))
    r_tr.append(mido.Message("note_on", channel=0, note=60, velocity=80, time=0))
    r_tr.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=480))
    mid.tracks.append(r_tr)
    # 左手:同通道 0、同程序 0
    l_tr = mido.MidiTrack()
    l_tr.append(mido.MetaMessage("track_name", name="Piano LH", time=0))
    l_tr.append(mido.Message("program_change", channel=0, program=0, time=0))
    l_tr.append(mido.Message("control_change", channel=0, control=64, value=127, time=0))
    l_tr.append(mido.Message("note_on", channel=0, note=36, velocity=70, time=0))
    l_tr.append(mido.Message("note_off", channel=0, note=36, velocity=0, time=480))
    mid.tracks.append(l_tr)

    groups = split_midi(mid)
    assert len(groups) == 1
    assert groups[0].channel == 0
    assert groups[0].program == 0
    # 合并为单声部 SMF
    out_mid = group_to_midi(groups[0], 0, 480)
    notes = [m for tr in out_mid.tracks for m in tr if m.type == "note_on"]
    cc64 = [
        m for tr in out_mid.tracks for m in tr if m.type == "control_change" and m.control == 64
    ]
    assert len(notes) == 2
    assert len(cc64) == 1


def test_prepare_end_to_end(tmp_path: Path):
    xml_path = tmp_path / "score.musicxml"
    xml_path.write_text(SIMPLE_XML, encoding="utf-8")
    outdir = tmp_path / "out"
    plan_path = prepare(xml_path, outdir)
    assert plan_path.exists()
    doc = json.loads(plan_path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert len(doc["parts"]) == 2
    assert doc["parts"][0]["id"] == "P1"
    assert doc["parts"][0]["file"] == "parts/P1.mid"
    assert (outdir / "parts" / "P1.mid").exists()
    assert (outdir / "parts" / "P2.mid").exists()


def test_invalid_input(tmp_path: Path):
    bad = tmp_path / "bad.txt"
    bad.write_text("not a score", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported input type"):
        prepare(bad, tmp_path / "out")

    bad_xml = tmp_path / "bad.xml"
    bad_xml.write_text("<root>not musicxml</root>", encoding="utf-8")
    with pytest.raises(ScoreError):
        prepare(bad_xml, tmp_path / "out")
