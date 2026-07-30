import fs from "node:fs/promises";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const OUT = "Z:/python/Projects/task/nl2sql-presearch/docs/NL2SQL_MVP_优化baseline汇报_2026-07-29.pptx";
const TMP = "Z:/python/Projects/task/nl2sql-presearch/.codex_tmp/nl2sql_ppt/render";

async function writeBlob(path, blob) {
  await fs.writeFile(path, new Uint8Array(await blob.arrayBuffer()));
}

function addText(slide, text, position, style = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = {
    fontSize: style.fontSize ?? 24,
    bold: style.bold ?? false,
    color: style.color ?? "#000000",
    fontFace: "Helvetica Neue",
    alignment: style.alignment ?? "left",
  };
  return shape;
}

function addRule(slide, top) {
  slide.shapes.add({
    geometry: "rect",
    position: { left: 72, top, width: 1136, height: 1.5 },
    fill: "#B8BCC4",
    line: { style: "solid", fill: "none", width: 0 },
  });
}

function addPanel(slide, left, top, width, height, fill = "#EDEDED") {
  return slide.shapes.add({
    geometry: "rect",
    position: { left, top, width, height },
    fill,
    line: { style: "solid", fill: "#D7DAE0", width: 1 },
  });
}

function addMetric(slide, left, top, label, value, note) {
  addPanel(slide, left, top, 250, 150);
  addText(slide, value, { left: left + 24, top: top + 24, width: 202, height: 52 }, {
    fontSize: 40,
    bold: true,
  });
  addText(slide, label, { left: left + 24, top: top + 82, width: 202, height: 30 }, {
    fontSize: 18,
    bold: true,
  });
  addText(slide, note, { left: left + 24, top: top + 114, width: 202, height: 24 }, {
    fontSize: 14,
    color: "#4B5563",
  });
}

function addNotes(slide, notes) {
  slide.speakerNotes.textFrame.setText(`[Sources]\n${notes}`);
}

const presentation = Presentation.create({
  slideSize: { width: 1280, height: 720 },
});

// Slide 1
{
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "NL2SQL MVP", { left: 72, top: 64, width: 520, height: 64 }, {
    fontSize: 52,
    bold: true,
  });
  addText(slide, "优化 baseline 已经达到可汇报状态", { left: 72, top: 184, width: 850, height: 92 }, {
    fontSize: 44,
    bold: true,
  });
  addText(slide, "2026-07-29 · Windows 本机 · 标准后端 8002", { left: 72, top: 548, width: 720, height: 32 }, {
    fontSize: 22,
    color: "#4B5563",
  });
  addPanel(slide, 914, 92, 220, 220, "#EAF6FF");
  addText(slide, "100%", { left: 944, top: 142, width: 160, height: 64 }, {
    fontSize: 54,
    bold: true,
    alignment: "center",
  });
  addText(slide, "33 / 33 golden cases", { left: 944, top: 224, width: 160, height: 44 }, {
    fontSize: 20,
    alignment: "center",
  });
  addNotes(slide, "training/xiyan3b_ollama_mvp_optimized_report.json; docs/NL2SQL_MVP_优化baseline测试报告_2026-07-29.md");
}

// Slide 2
{
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "Baseline 结果说明 LoRA 不是当前瓶颈", { left: 72, top: 48, width: 1050, height: 58 }, {
    fontSize: 38,
    bold: true,
  });
  addRule(slide, 130);
  addMetric(slide, 72, 188, "通过率", "100%", "33 条全通过");
  addMetric(slide, 354, 188, "平均耗时", "2.427s", "端到端 wall time");
  addMetric(slide, 636, 188, "P50", "1.708s", "中位响应");
  addMetric(slide, 918, 188, "P95", "4.354s", "尾部延迟");
  addText(slide, "当前收益主要来自 prompt 压缩、metadata cache、结果摘要模板和数据源专属约束；不是通过训练获得。", { left: 90, top: 426, width: 1060, height: 72 }, {
    fontSize: 26,
  });
  addNotes(slide, "training/xiyan3b_ollama_mvp_optimized_report.json");
}

// Slide 3
{
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "8 个数据源覆盖了核心 MVP 演示面", { left: 72, top: 48, width: 980, height: 58 }, {
    fontSize: 38,
    bold: true,
  });
  addRule(slide, 130);
  slide.charts.add("bar", {
    position: { left: 88, top: 178, width: 760, height: 388 },
    categories: ["SQLite", "MySQL", "Postgres", "Gauss", "Dameng", "Hadoop", "REST", "GraphQL"],
    series: [{ name: "通过用例", values: [4, 4, 6, 4, 4, 4, 3, 4], fill: "#3D8DFF" }],
    hasLegend: false,
    dataLabels: { showValue: true, position: "outEnd" },
    yAxis: { majorGridlines: { style: "solid", fill: "#E5E7EB", width: 1 } },
  });
  addText(slide, "所有数据源均已通过当前 golden cases。PostgreSQL 股票库保持数据源名 postgres_stock，并使用本地 pg-local 容器。", { left: 900, top: 194, width: 280, height: 180 }, {
    fontSize: 24,
  });
  addText(slide, "慢点主要集中在 Dameng 与 Hadoop，属于适配器/本地演示执行路径和模型输出波动，不是单一模型能力问题。", { left: 900, top: 414, width: 280, height: 130 }, {
    fontSize: 20,
    color: "#4B5563",
  });
  addNotes(slide, "training/xiyan3b_ollama_mvp_optimized_report.json; /api/v1/data-sources validation on 2026-07-29");
}

// Slide 4
{
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "剩余错误用工程修正解决，而不是先训练", { left: 72, top: 48, width: 1060, height: 58 }, {
    fontSize: 38,
    bold: true,
  });
  addRule(slide, 130);
  addPanel(slide, 86, 190, 500, 260);
  addText(slide, "Hadoop 月度趋势", { left: 116, top: 220, width: 430, height: 36 }, {
    fontSize: 26,
    bold: true,
  });
  addText(slide, "失败原因：模型生成 TO_DATE / DATE_FORMAT，但本地演示适配器用 SQLite 执行 CSV。", { left: 116, top: 286, width: 420, height: 74 }, {
    fontSize: 20,
  });
  addText(slide, "修正：XiYan prompt 明确使用 substr(event_date, 1, 7)。", { left: 116, top: 380, width: 420, height: 48 }, {
    fontSize: 20,
    color: "#374151",
  });
  addPanel(slide, 694, 190, 500, 260);
  addText(slide, "警务地址别名", { left: 724, top: 220, width: 430, height: 36 }, {
    fontSize: 26,
    bold: true,
  });
  addText(slide, "失败原因：旧 few-shot 使用不存在的 addr_alias.std_address_id。", { left: 724, top: 286, width: 420, height: 74 }, {
    fontSize: 20,
  });
  addText(slide, "修正：改为 std_address_code 关联标准地址，并保留 LIKE 过滤。", { left: 724, top: 380, width: 420, height: 48 }, {
    fontSize: 20,
    color: "#374151",
  });
  addNotes(slide, "backend/nl2sql/prompt_builder.py; training/xiyan3b_ollama_mvp_optimized_report.json failure analysis");
}

// Slide 5
{
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "审计链路已经能支撑问题复盘", { left: 72, top: 48, width: 980, height: 58 }, {
    fontSize: 38,
    bold: true,
  });
  addRule(slide, 130);
  addPanel(slide, 116, 178, 620, 344);
  addText(slide, "审计字段", { left: 146, top: 202, width: 300, height: 30 }, {
    fontSize: 22,
    bold: true,
  });
  addText(slide, "覆盖情况", { left: 510, top: 202, width: 160, height: 30 }, {
    fontSize: 22,
    bold: true,
  });
  const rows = [
    ["generated_sql", "33 / 33"],
    ["executed_sql", "33 / 33"],
    ["result_sample_json", "33 / 33"],
    ["stage_timings_json", "33 / 33"],
    ["raw_model_output", "30 / 33"],
    ["完整五阶段耗时", "30 / 33"],
  ];
  let rowTop = 254;
  for (const [field, coverage] of rows) {
    addText(slide, field, { left: 146, top: rowTop, width: 300, height: 26 }, {
      fontSize: 18,
    });
    addText(slide, coverage, { left: 510, top: rowTop, width: 160, height: 26 }, {
      fontSize: 18,
      bold: true,
    });
    rowTop += 42;
  }
  addText(slide, "3 条 REST API 用例走服务编排，不经过 SQL 模型生成，所以没有 raw_model_output 和 SQL generation 阶段；其 API 执行动作、样本和总耗时仍已记录。", { left: 790, top: 198, width: 330, height: 190 }, {
    fontSize: 22,
  });
  addText(slide, "view_audit.py 已修复 Windows 控制台 Unicode 输出问题。", { left: 790, top: 438, width: 330, height: 72 }, {
    fontSize: 20,
    color: "#4B5563",
  });
  addNotes(slide, "data/audit/2026-07-29/audit_2026-07-29.db direct SQLite audit check; scripts/view_audit.py");
}

// Slide 6
{
  const slide = presentation.slides.add();
  slide.background.fill = "#FFFFFF";
  addText(slide, "下一步是扩大评估集，再决定是否 QLoRA", { left: 72, top: 48, width: 1060, height: 58 }, {
    fontSize: 38,
    bold: true,
  });
  addRule(slide, 130);
  const steps = [
    ["01", "扩展 golden cases 到 80-120 条，提高字段、过滤和分组断言强度。"],
    ["02", "只把人工校验 SQL 写入 training/sft_train.jsonl。"],
    ["03", "用相同独立评估集比较微调前后通过率、语义正确率和 P50/P95。"],
  ];
  let top = 190;
  for (const [num, text] of steps) {
    addText(slide, num, { left: 96, top, width: 90, height: 48 }, {
      fontSize: 34,
      bold: true,
      color: "#3D8DFF",
    });
    addText(slide, text, { left: 200, top: top + 2, width: 880, height: 58 }, {
      fontSize: 26,
    });
    top += 118;
  }
  addText(slide, "当前不建议直接训练：MVP baseline 已全通过，训练收益要靠新增独立集证明。", { left: 96, top: 560, width: 940, height: 48 }, {
    fontSize: 24,
    color: "#374151",
  });
  addNotes(slide, "docs/NL2SQL_微调数据与QLoRA方案_2026-07-29.md; training/train_qlora_xiyan3b.ps1");
}

await fs.mkdir(TMP, { recursive: true });
for (const [index, slide] of presentation.slides.items.entries()) {
  const stem = `slide-${String(index + 1).padStart(2, "0")}`;
  await writeBlob(`${TMP}/${stem}.png`, await presentation.export({ slide, format: "png", scale: 1 }));
  await fs.writeFile(`${TMP}/${stem}.layout.json`, await (await slide.export({ format: "layout" })).text());
}
await writeBlob(`${TMP}/deck-montage.webp`, await presentation.export({ format: "webp", montage: true, scale: 1 }));
const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(OUT);
console.log(OUT);
