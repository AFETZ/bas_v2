#!/usr/bin/env python3
"""Build a separate anonymous revision from real analysis inputs or explicit gaps.

Run inside the existing document-tools image under the BAS runtime lock. This
script reads old evidence/styles and never writes the previous paper/build.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

HERE = Path(__file__).resolve().parent
PAPER = HERE.parent
SPEC = importlib.util.spec_from_file_location("old_paper_styles", PAPER / "tools/build_docx.py")
STYLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STYLE)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolved(path, parent):
    path = Path(path)
    return path if path.is_absolute() else (parent / path).resolve()


def fmt(value, places=3):
    return "—" if value is None or value == "" else f"{float(value):.{places}f}".replace(".", ",")


class Revision:
    def __init__(self, args):
        self.args = args
        self.inputs = {}
        self.base = args.manifest.resolve().parent if args.manifest else HERE
        self.manifest = self.read_json(args.manifest) if args.manifest else {"kind": "preparation"}
        self.kind = self.manifest.get("kind", "preparation")
        if self.kind not in ("preparation", "engineering_pilot", "partial_series", "frozen_series"):
            raise ValueError("unknown dataset kind")
        self.runs = []
        for index, entry in enumerate(self.manifest.get("analyses", []), 1):
            path = resolved(entry["path"], self.base)
            result = self.read_json(path)
            self.runs.append((entry.get("label", f"U{index:02d}"), entry.get("group", "unspecified"), result))
        if args.final:
            if self.kind != "frozen_series" or not self.runs:
                raise ValueError("--final requires frozen_series with actual analysis inputs")
            freeze = resolved(self.manifest["freeze_path"], self.base)
            self.read_json(freeze)
            if not self.manifest.get("source_commit"):
                raise ValueError("frozen version is required")
            for label, _group, run in self.runs:
                checks = run.get("physical_checks", {})
                full_window = run.get("observation_window", {}).get("complete") is True
                if (checks.get("total") != checks.get("verified") or (full_window and
                        (run.get("data_quality_errors") or run.get("missing_measurements")))):
                    raise ValueError(f"{label}: unresolved recording/physical-evidence issue; keep draft")
        self.doc = Document()
        STYLE.configure(self.doc)
        self.doc.sections[0].header.paragraphs[0].text = "Восстановление управления и геометрическое экранирование"
        self.doc.core_properties.title = "Anonymous UAV relay manuscript: geometry extension"
        self.doc.core_properties.modified = datetime(2026, 10, 28, tzinfo=timezone.utc)
        self.table_number, self.figure_number = 0, 0

    def read_json(self, path):
        path = Path(path).resolve()
        self.inputs[str(path)] = sha(path)
        return json.loads(path.read_text())

    def para(self, text, style="ispText_main"):
        return STYLE.p(self.doc, text, style)

    def table(self, heads, rows, caption_ru, caption_en):
        self.table_number += 1
        for text in (f"Табл. {self.table_number}. {caption_ru}", f"Table {self.table_number}. {caption_en}"):
            p = self.para(text, "ispPicture_sign")
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.keep_with_next = True
        table = self.doc.add_table(rows=1, cols=len(heads))
        table.style = "Table Grid"
        for cell, text in zip(table.rows[0].cells, heads):
            cell.text = str(text)
        for row in rows:
            if len(row) != len(heads):
                raise ValueError("table column count mismatch")
            for cell, text in zip(table.add_row().cells, row):
                cell.text = "—" if text is None or text == "" else str(text)
        for i, row in enumerate(table.rows):
            for cell in row.cells:
                for p in cell.paragraphs:
                    p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(2)
                    for run in p.runs:
                        run.font.name, run.font.size, run.bold = "Times New Roman", Pt(9), i == 0

    def figure(self, path, caption_ru, caption_en):
        path = Path(path).resolve()
        self.inputs[str(path)] = sha(path)
        from PIL import Image
        with Image.open(path) as image:
            if image.width / (13.9 / 2.54) < 300:
                raise ValueError(f"figure has less than 300 effective dpi: {path}")
        self.figure_number += 1
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(str(path), width=Cm(13.9))
        STYLE.figure_caption(self.doc, f"Рис. {self.figure_number}. {caption_ru}")
        STYLE.figure_caption(self.doc, f"Fig. {self.figure_number}. {caption_en}", english=True)

    def methodology(self):
        rows = self.manifest.get("methodology", [
            ["Политика", "fixed / failover", "host-monotonic", "параметры протокола сохранены"],
            ["Радиоконтроль", "geometry / LOS-only", "native ns-3", "одна физическая сцена"],
            ["Повторения", "НЕ ЗАФИКСИРОВАНЫ", "отдельные блоки", "требуется freeze до серии"],
            ["Горизонт", "НЕ ЗАФИКСИРОВАН", "часы источника поз", "до просмотра эффекта политики"],
            ["Цели", "последовательные waypoint", "автопилот time_boot_ms", "доставленная по радио телеметрия"],
            ["Timely", "250 мс", "command.created → UART", "offered остаётся знаменателем"],
            ["Модель канала", "эмпирические гипотезы", "actual geometry", "полевая калибровка не заявляется"],
        ])
        self.table(["Фактор", "Значение", "Шкала / источник", "Ограничение"], rows,
                   "Методика новой серии; незаполненные параметры не являются результатами.",
                   "New-study methodology; unresolved settings are not experimental results.")

    def legacy_table(self):
        path = PAPER / "data/six_cells.csv"
        self.inputs[str(path)] = sha(path)
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        self.table(["Отказ", "Политика", "Timely, %", "T_UART, с", "T_goal, с"],
            [[r["condition"], r["policy"], fmt(100*float(r["mean_timely_per_offered"])),
              fmt(r["mean_actual_control_s"]), fmt(r["mean_actual_goal_s"])] for r in rows],
            "Сохранённая вертикальная основная серия: пять запусков в каждой ячейке; Friis и заданный отказ.",
            "Retained vertical main study: five runs per cell; Friis and declared injected loss.")

    def results(self):
        descriptions = {
            "preparation": "Новая основная городская серия в данном черновике не представлена. Численные эффекты политики, интервалы и вывод об успешном восстановлении не заполнены. Это каркас методики и представления результатов.",
            "engineering_pilot": "Ниже представлены инженерные пилоты. Они не включены в оценку эффекта замороженной основной серии и не устанавливают воспроизводимый средний выигрыш failover.",
            "partial_series": "Ниже представлены доступные попытки ещё не завершённой серии. Неуспешные и неполные окна сохранены. Итоговый эффект по завершённому плану пока не заявляется.",
            "frozen_series": "Ниже представлены попытки отдельно замороженной городской серии. Статус запуска, полнота окна, отсутствие события и цензурирование остаются различимыми. Наличие failed не является основанием удалить попытку.",
        }
        self.para(descriptions[self.kind], "ispAnotation")
        if self.manifest.get("source_commit"):
            self.para("Версия новой реализации: " + str(self.manifest["source_commit"]) + ".")
        if self.manifest.get("probe_review"):
            probe = self.read_json(resolved(self.manifest["probe_review"], self.base))
            self.para("Компонентные проверки ниже относятся к неподвижным узлам и native Wi-Fi. Они проверяют реализацию геометрии/потерь и пакетного пути, но не заменяют пятибортовый полёт либо полевую калибровку.")
            self.table(["Проба", "LOS geometry", "Loss, dB", "Direct", "Via relay"],
                [[r["case"], r["geom_los"], fmt(r["loss_db"]), r["direct_received_unique"],
                  r["via_relay_received_unique"]] for r in probe["results"]],
                "Реальные компонентные пробы; уникальные sequence IDs проверены по PCAP.",
                "Measured component probes; unique sequence IDs checked against PCAP.")
        if self.runs:
            rows = []
            for label, group, run in self.runs:
                rate = run.get("rates", {}).get("all_five", {})
                endpoint = run.get("uart_after_NLOS", {}).get("all_five", {})
                time = fmt(endpoint.get("time_s")) if endpoint.get("status") == "observed" else {
                    "administratively_right_censored": ">" + fmt(endpoint.get("censor_time_s")),
                    "not_applicable_no_observed_NLOS_transition": "н/п: нет перехода",
                    "unverifiable_recording": "нет данных",
                    "interrupted_attempt": "прервано",
                }.get(endpoint.get("status"), "—")
                progress = list(run.get("mission_progress", {}).values())
                planned = sum(r["planned_waypoints"] for r in progress) if progress and all(
                    r.get("planned_waypoints") is not None for r in progress) else None
                done = sum(r["completed_waypoints"] for r in progress) if progress else None
                points = f"{done}/{planned}" if planned is not None and done is not None else "—"
                modes = "/".join(run.get("geometry", {}).get("selected_radio_modes", [])) or "—"
                timely = rate.get("timely_per_offered")
                rows.append([f"{label} ({group})", run.get("policy", "—") + "/" + modes,
                    run.get("recorded_status", "—"), fmt(100*timely) if timely is not None else "—",
                    time, points])
            self.table(["Опыт / группа", "Политика / radio", "Статус", "Timely, %", "После NLOS, с", "Точки / план"], rows,
                "Новые наблюдения по отдельным попыткам. Время после NLOS не называется восстановлением без подтверждённой деградации.",
                "New per-attempt observations. Post-NLOS latency is not called recovery without observed impairment.")
        for table in self.manifest.get("tables", []):
            path = resolved(table["csv"], self.base)
            self.inputs[str(path)] = sha(path)
            with path.open() as stream:
                source = list(csv.reader(stream))
            if not source:
                raise ValueError("empty numerical table")
            self.table(source[0], source[1:], table["caption_ru"], table["caption_en"])

    def figures(self):
        for entry in self.manifest.get("figures", []):
            if entry.get("measured") is not True or not self.runs:
                raise ValueError("urban figures require explicit measured:true and actual analysis inputs")
            self.figure(resolved(entry["path"], self.base), entry["caption_ru"], entry["caption_en"])

    def build(self):
        source = HERE / "src" / ("supplement.md" if self.args.supplement else "manuscript.md")
        self.inputs[str(source)] = sha(source)
        self.inputs[str(Path(__file__))] = sha(Path(__file__))
        for path in (PAPER / "tools/build_docx.py", PAPER / "template/official_russian_template.dotm"):
            self.inputs[str(path)] = sha(path)
        for line in source.read_text().splitlines():
            if not line.strip():
                continue
            prefixes = (("@title ", "ispHeader", ""), ("@title_en ", "ispHeader1", ""),
                ("@abstract ", "ispAnotation", "Аннотация. "), ("@abstract_en ", "ispAnotation", "Abstract. "),
                ("@keywords ", "ispAnotation", "Ключевые слова: "), ("@keywords_en ", "ispAnotation", "Keywords: "),
                ("## ", "ispSubHeader-3 level", ""), ("# ", "ispSubHeader-2 level", ""))
            match = next(((prefix, style, extra) for prefix, style, extra in prefixes if line.startswith(prefix)), None)
            if match:
                prefix, style, extra = match
                self.para(extra + line[len(prefix):], style)
            elif line == "@table methodology":
                self.methodology()
            elif line == "@legacy_table":
                self.legacy_table()
            elif line == "@legacy_figures":
                self.figure(PAPER / "figures/paired.png",
                    "Сохранённая вертикальная серия: парные эффекты и разведочные интервалы. Это отдельная когорта.",
                    "Retained vertical study: paired effects and exploratory intervals. This is a separate cohort.")
            elif line == "@results":
                self.results()
            elif line == "@figures":
                self.figures()
            elif line == "@references":
                self.para("Список литературы / References", "ispSubHeader-2 level")
                refs = HERE / "references.txt"
                self.inputs[str(refs)] = sha(refs)
                for ref in refs.read_text().splitlines():
                    if not self.args.supplement or int(ref.split("]")[0][1:]) <= 6:
                        self.para(ref, "ispLitList")
            elif line.startswith("@"):
                raise ValueError("unsupported manuscript directive: " + line)
            else:
                self.para(line)
        output = self.args.output.resolve()
        if output == PAPER / "build" or PAPER / "build" in output.parents:
            raise ValueError("previous paper/build is immutable")
        output.mkdir(parents=True, exist_ok=True)
        dest = output / "manuscript_anonymous.docx"
        self.doc.save(dest)
        with ZipFile(dest) as package:
            for name in package.namelist():
                if "vbaProject" in name or "attachedTemplate" in name:
                    raise ValueError("unexpected macro/template part")
                if name.endswith((".xml", ".rels")):
                    value = package.read(name)
                    if b"/home/" in value or b"file:///" in value or b"attachedTemplate" in value:
                        raise ValueError("local path/template link in anonymous document")
        result = dict(dataset_kind=self.kind, final_requested=self.args.final, input_sha256=self.inputs,
            analyses=len(self.runs), tables=self.table_number, figures=self.figure_number,
            docx_sha256=sha(dest), old_paper_build_written=False,
            note="document rendering validates presentation inputs, not scientific truth or model calibration")
        (output / "build_manifest.json").write_text(json.dumps(result, indent=2) + "\n")
        print(dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "build")
    parser.add_argument("--supplement", action="store_true")
    parser.add_argument("--final", action="store_true")
    Revision(parser.parse_args()).build()


if __name__ == "__main__":
    main()
