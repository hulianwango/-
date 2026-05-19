import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  FileText,
  RefreshCw,
  Save,
  Search,
  X,
} from "lucide-react";
import * as pdfjsLib from "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.mjs";

const LIBRARY_LIST_LIMIT = 5000;

const TEXT_FIELDS = [
  ["main_work", "本文做了什么"],
  ["material_system", "材料体系"],
  ["methods", "实验方法"],
  ["key_results", "关键结果"],
  ["mechanisms", "机制"],
  ["evidence", "证据"],
  ["page_numbers", "页码"],
  ["relevance_to_project", "与课题关系"],
  ["recommended_tags", "推荐标签"],
  ["limitations", "局限"],
  ["mechanism_summary", "机制摘要"],
];

const PROJECT_FIELDS = [
  ["is_colloid", "胶体"],
  ["involves_520_nm", "520 nm"],
  ["involves_540_nm", "540 nm"],
  ["involves_red_emission", "红光发射"],
  ["supports_red_emission_design", "支持红光设计"],
  ["warns_green_channel_enhancement", "警惕绿光增强"],
];

const NUMBER_FIELDS = [
  ["confidence", "置信度"],
  ["au_size_nm", "Au 尺寸 nm"],
  ["lspr_peak_nm", "LSPR 峰 nm"],
  ["relevance_score", "相关分"],
];

const SHORT_TEXT_FIELDS = [
  ["au_shape", "Au 形貌"],
  ["er_host", "Er 基质"],
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json();
}

function asEditableValue(value) {
  if (Array.isArray(value) || (value && typeof value === "object")) {
    return JSON.stringify(value, null, 2);
  }
  return value ?? "";
}

function parseEditableValue(raw) {
  const value = raw.trim();
  if (!value) return "";
  if (value.startsWith("[") || value.startsWith("{")) {
    try {
      return JSON.parse(value);
    } catch {
      return raw;
    }
  }
  return raw;
}

function decodeEntities(value) {
  if (value === null || value === undefined) return "";
  const textarea = document.createElement("textarea");
  textarea.innerHTML = String(value);
  return textarea.value;
}

function compactMeta(...values) {
  return values.map(decodeEntities).filter(Boolean).join(" · ");
}

function normalizedList(value) {
  return Array.isArray(value) ? value.map(decodeEntities).filter(Boolean) : [];
}

function BilingualLabel({ zh, en }) {
  return (
    <span className="bilingualLabel">
      <span>{zh}</span>
      <small>{en}</small>
    </span>
  );
}

function LanguagePanel({ title, children, notice = false, className = "" }) {
  return (
    <article className={`languagePanel ${className}`.trim()}>
      <strong>{title}</strong>
      <p className={notice ? "summaryNotice" : "summaryText"}>{children}</p>
    </article>
  );
}

function PaperList({ papers, query, setQuery, onSearch, onScan, selectedId, onSelect, busy }) {
  return (
    <aside className="sidebar">
      <div className="toolbar">
        <div className="searchBox">
          <Search size={17} aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onSearch();
            }}
            placeholder="搜索标题、DOI、正文"
          />
        </div>
        <button className="iconButton" onClick={onSearch} title="搜索" aria-label="搜索">
          <Search size={17} />
        </button>
        <button className="iconButton" onClick={onScan} title="扫描" aria-label="扫描" disabled={busy}>
          <RefreshCw size={17} className={busy ? "spin" : ""} />
        </button>
      </div>
      <div className="libraryMeta">
        <strong>{query.trim() ? "搜索结果" : "全部文献"}</strong>
        <span>{papers.length} 篇</span>
      </div>
      <div className="paperList">
        {papers.map((paper) => {
          const title = decodeEntities(paper.title || "Untitled");
          return (
            <button
              className={`paperRow ${selectedId === paper.paper_id ? "selected" : ""}`}
              key={`${paper.paper_id}-${paper.chunk_id || "paper"}`}
              onClick={() => onSelect(paper)}
            >
              <FileText size={17} aria-hidden="true" />
              <span>
                <strong>{title}</strong>
                <small>{compactMeta(paper.authors, paper.year, paper.doi)}</small>
                {paper.snippet ? <em>{decodeEntities(paper.snippet)}</em> : null}
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function PdfReader({ paper }) {
  const canvasRef = useRef(null);
  const [pdfDoc, setPdfDoc] = useState(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [message, setMessage] = useState("");

  useEffect(() => {
    let cancelled = false;
    setPdfDoc(null);
    setPageNumber(1);
    setPageCount(0);
    setMessage("");
    if (!paper) return;

    const task = pdfjsLib.getDocument(`/local/papers/${paper.paper_id}/pdf`);
    task.promise
      .then((doc) => {
        if (cancelled) return;
        setPdfDoc(doc);
        setPageCount(doc.numPages);
      })
      .catch((error) => {
        if (!cancelled) setMessage(error.message);
      });

    return () => {
      cancelled = true;
      task.destroy();
    };
  }, [paper]);

  useEffect(() => {
    let cancelled = false;
    async function renderPage() {
      if (!pdfDoc || !canvasRef.current) return;
      const page = await pdfDoc.getPage(pageNumber);
      if (cancelled) return;
      const container = canvasRef.current.parentElement;
      const baseViewport = page.getViewport({ scale: 1 });
      const width = Math.max(320, container.clientWidth - 24);
      const scale = Math.min(1.7, width / baseViewport.width);
      const viewport = page.getViewport({ scale });
      const canvas = canvasRef.current;
      const context = canvas.getContext("2d");
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      await page.render({ canvasContext: context, viewport }).promise;
    }
    renderPage();
    return () => {
      cancelled = true;
    };
  }, [pdfDoc, pageNumber]);

  if (!paper) {
    return (
      <main className="reader emptyState">
        <BookOpen size={38} />
        <span>选择文献</span>
      </main>
    );
  }

  const title = decodeEntities(paper.title || "Untitled");

  return (
    <main className="reader">
      <div className="readerHeader">
        <div>
          <h1>{title}</h1>
          <p>{compactMeta(paper.authors, paper.year, paper.doi)}</p>
        </div>
        <div className="pager">
          <button
            className="iconButton"
            title="上一页"
            aria-label="上一页"
            onClick={() => setPageNumber((value) => Math.max(1, value - 1))}
            disabled={pageNumber <= 1}
          >
            <ChevronLeft size={18} />
          </button>
          <span>
            {pageNumber}/{pageCount || "-"}
          </span>
          <button
            className="iconButton"
            title="下一页"
            aria-label="下一页"
            onClick={() => setPageNumber((value) => Math.min(pageCount || value, value + 1))}
            disabled={!pageCount || pageNumber >= pageCount}
          >
            <ChevronRight size={18} />
          </button>
        </div>
      </div>
      {message ? <div className="inlineError">{message}</div> : null}
      <div className="canvasWrap">
        <canvas ref={canvasRef} />
      </div>
    </main>
  );
}

function PaperOverview({ paper, overview, loading, error }) {
  if (!paper) {
    return (
      <section className="paperOverview emptyState">
        <BookOpen size={34} />
        <span>选择左侧文献</span>
      </section>
    );
  }

  const title = decodeEntities(overview?.title || paper.title || "Untitled");
  const authors = decodeEntities(overview?.authors || paper.authors || "");
  const doi = decodeEntities(overview?.doi || paper.doi || "");
  const pageCount = overview?.page_count ?? paper.page_count;
  const abstractEn = decodeEntities(overview?.abstract_text || "");
  const overviewZh = decodeEntities(overview?.auto_chinese_overview || "");
  const overviewEn = decodeEntities(overview?.auto_english_overview || abstractEn);
  const summaryZh = decodeEntities(
    overview?.has_chinese_summary
      ? overview.chinese_summary
      : overview?.auto_chinese_abstract || overview?.auto_chinese_overview || "",
  );
  const summaryZhMissing =
    overview?.missing_chinese_reason || "这篇文献还没有保存中文摘要，也暂时无法生成自动中文摘要。";
  const mainPointsZh = normalizedList(overview?.main_points_zh);
  const mainPointsEn = normalizedList(overview?.main_points_en || overview?.main_points);
  const mainPointCount = Math.max(mainPointsZh.length, mainPointsEn.length);
  const pairedMainPoints = Array.from({ length: mainPointCount }, (_, index) => ({
    zh: mainPointsZh[index] || "",
    en: mainPointsEn[index] || "",
  }));
  const headings = overview?.section_headings || [];

  return (
    <section className="paperOverview">
      <div className="overviewHeader">
        <div>
          <h2>
            <BilingualLabel zh="本文主要内容" en="Main Content" />
          </h2>
          <p>{title}</p>
        </div>
        {loading ? <span className="statusPill">读取中</span> : null}
      </div>

      <div className="overviewBody">
        {error ? <div className="inlineError">{error}</div> : null}

        <section className="overviewBlock">
          <h3>
            <BilingualLabel zh="完整题录" en="Full Record" />
          </h3>
          <dl className="metaGrid">
            <div>
              <dt>
                <BilingualLabel zh="年份" en="Year" />
              </dt>
              <dd>{overview?.year || paper.year || "未知"}</dd>
            </div>
            <div>
              <dt>
                <BilingualLabel zh="页数" en="Pages" />
              </dt>
              <dd>{pageCount || "未知"}</dd>
            </div>
            <div>
              <dt>DOI</dt>
              <dd>{doi || "未识别"}</dd>
            </div>
            <div>
              <dt>
                <BilingualLabel zh="作者" en="Authors" />
              </dt>
              <dd>{authors || "未识别"}</dd>
            </div>
          </dl>
        </section>

        <section className="overviewBlock">
          <h3>
            <BilingualLabel zh="摘要" en="Abstract" />
          </h3>
          <div className="languagePair">
            <LanguagePanel title="中文" notice={!summaryZh}>
              {summaryZh || summaryZhMissing}
            </LanguagePanel>
            <LanguagePanel title="English" notice={!abstractEn} className="originalText">
              {abstractEn || "No English abstract was detected from the indexed PDF text."}
            </LanguagePanel>
          </div>
        </section>

        {overviewZh || overviewEn ? (
          <section className="overviewBlock">
            <h3>
              <BilingualLabel zh="自动概览" en="Auto Overview" />
            </h3>
            <div className="languagePair">
              <LanguagePanel title="中文" notice={!overviewZh}>
                {overviewZh || "暂时没有自动中文概览。"}
              </LanguagePanel>
              <LanguagePanel title="English" notice={!overviewEn} className="originalText">
                {overviewEn || "No English overview is available."}
              </LanguagePanel>
            </div>
          </section>
        ) : null}

        <section className="overviewBlock">
          <h3>
            <BilingualLabel zh="主要内容" en="Key Points" />
          </h3>
          {pairedMainPoints.length ? (
            <ul className="mainPointList bilingualPointList">
              {pairedMainPoints.map((point, index) => (
                <li key={`${index}-${point.zh || point.en}`}>
                  {point.zh ? (
                    <div className="pointLanguage">
                      <strong>中文</strong>
                      <p>{point.zh}</p>
                    </div>
                  ) : null}
                  {point.en ? (
                    <div className="pointLanguage originalText">
                      <strong>English</strong>
                      <p>{point.en}</p>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="summaryNotice">
              未能从索引正文中提取主要内容。 / No key points were extracted from the indexed text.
            </p>
          )}
        </section>

        {headings.length ? (
          <section className="overviewBlock">
            <h3>
              <BilingualLabel zh="章节线索" en="Section Clues" />
            </h3>
            <div className="headingList">
              {headings.map((heading) => (
                <span key={`${heading.page_number}-${heading.text}`}>
                  P{heading.page_number} · {decodeEntities(heading.text)}
                </span>
              ))}
            </div>
          </section>
        ) : null}
      </div>
    </section>
  );
}

function DraftEditor({ draft, onSave, onAccept, onReject }) {
  const [annotation, setAnnotation] = useState({});

  useEffect(() => {
    setAnnotation(draft?.annotation_json || {});
  }, [draft]);

  const setField = (key, value) => {
    setAnnotation((current) => ({ ...current, [key]: value }));
  };

  if (!draft) {
    return (
      <aside className="draftPanel emptyState">
        <FileText size={34} />
        <span>待审草稿</span>
      </aside>
    );
  }

  return (
    <aside className="draftPanel">
      <div className="draftHeader">
        <div>
          <h2>AI 草稿</h2>
          <p>{draft.title}</p>
        </div>
        <span className={`statusPill ${draft.status}`}>{draft.status}</span>
      </div>

      <div className="draftFields">
        {TEXT_FIELDS.map(([key, label]) => (
          <label key={key} className="field">
            <span>{label}</span>
            <textarea
              value={asEditableValue(annotation[key])}
              onChange={(event) => setField(key, parseEditableValue(event.target.value))}
              rows={key === "evidence" ? 5 : 3}
            />
          </label>
        ))}

        <div className="twoCol">
          {SHORT_TEXT_FIELDS.map(([key, label]) => (
            <label key={key} className="field">
              <span>{label}</span>
              <input value={annotation[key] ?? ""} onChange={(event) => setField(key, event.target.value)} />
            </label>
          ))}
        </div>

        <div className="twoCol">
          {NUMBER_FIELDS.map(([key, label]) => (
            <label key={key} className="field">
              <span>{label}</span>
              <input
                type="number"
                step="0.01"
                value={annotation[key] ?? ""}
                onChange={(event) =>
                  setField(key, event.target.value === "" ? "" : Number(event.target.value))
                }
              />
            </label>
          ))}
        </div>

        <div className="checkGrid">
          {PROJECT_FIELDS.map(([key, label]) => (
            <label key={key} className="checkItem">
              <input
                type="checkbox"
                checked={Boolean(annotation[key])}
                onChange={(event) => setField(key, event.target.checked)}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="draftActions">
        <button onClick={() => onSave(draft.draft_id, annotation)} disabled={draft.status !== "pending"}>
          <Save size={17} />
          保存
        </button>
        <button className="accept" onClick={() => onAccept(draft.draft_id)} disabled={draft.status !== "pending"}>
          <Check size={17} />
          接受
        </button>
        <button className="reject" onClick={() => onReject(draft.draft_id)} disabled={draft.status !== "pending"}>
          <X size={17} />
          拒绝
        </button>
      </div>
    </aside>
  );
}

function App() {
  const [papers, setPapers] = useState([]);
  const [drafts, setDrafts] = useState([]);
  const [query, setQuery] = useState("");
  const [selectedPaper, setSelectedPaper] = useState(null);
  const [selectedDraftId, setSelectedDraftId] = useState("");
  const [overview, setOverview] = useState(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const selectedDraft = useMemo(
    () =>
      drafts.find((draft) => draft.draft_id === selectedDraftId) ||
      drafts.find((draft) => draft.paper_id === selectedPaper?.paper_id) ||
      null,
    [drafts, selectedDraftId, selectedPaper?.paper_id],
  );

  const selectPaper = useCallback(
    (paper) => {
      setSelectedPaper(paper);
      const draftForPaper = drafts.find((draft) => draft.paper_id === paper.paper_id);
      setSelectedDraftId(draftForPaper?.draft_id || "");
    },
    [drafts],
  );

  const loadPapers = useCallback(async () => {
    const trimmedQuery = query.trim();
    const params = trimmedQuery
      ? `?query=${encodeURIComponent(trimmedQuery)}`
      : `?limit=${LIBRARY_LIST_LIMIT}`;
    const loaded = await api(`/local/papers${params}`);
    setPapers(loaded);
    setSelectedPaper((current) =>
      current && loaded.some((paper) => paper.paper_id === current.paper_id)
        ? current
        : loaded[0] || null,
    );
  }, [query]);

  const loadDrafts = useCallback(async () => {
    setDrafts(await api("/local/drafts"));
  }, []);

  useEffect(() => {
    loadPapers().catch((error) => setNotice(error.message));
    loadDrafts().catch((error) => setNotice(error.message));
  }, []);

  useEffect(() => {
    if (!selectedPaper?.paper_id) {
      setOverview(null);
      setOverviewError("");
      return;
    }

    let cancelled = false;
    setOverviewLoading(true);
    setOverviewError("");
    api(`/local/papers/${selectedPaper.paper_id}/overview`)
      .then((result) => {
        if (!cancelled) setOverview(result);
      })
      .catch((error) => {
        if (!cancelled) {
          setOverview(null);
          setOverviewError(error.message);
        }
      })
      .finally(() => {
        if (!cancelled) setOverviewLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedPaper?.paper_id, drafts]);

  const scan = async () => {
    setBusy(true);
    setNotice("");
    try {
      const result = await api("/local/scan", { method: "POST" });
      setNotice(`扫描 ${result.scanned}，入库 ${result.indexed}，失败 ${result.failed}`);
      await loadPapers();
    } catch (error) {
      setNotice(error.message);
    } finally {
      setBusy(false);
    }
  };

  const saveDraft = async (draftId, annotation) => {
    await api(`/local/drafts/${draftId}`, {
      method: "PUT",
      body: JSON.stringify({ annotation_json: annotation }),
    });
    await loadDrafts();
    setNotice("已保存");
  };

  const acceptDraft = async (draftId) => {
    await api(`/local/drafts/${draftId}/accept`, { method: "POST" });
    await loadDrafts();
    setNotice("已接受");
  };

  const rejectDraft = async (draftId) => {
    await api(`/local/drafts/${draftId}/reject`, { method: "POST" });
    await loadDrafts();
    setNotice("已拒绝");
  };

  return (
    <div className="appShell">
      <PaperList
        papers={papers}
        query={query}
        setQuery={setQuery}
        onSearch={() => loadPapers().catch((error) => setNotice(error.message))}
        onScan={scan}
        selectedId={selectedPaper?.paper_id}
        onSelect={selectPaper}
        busy={busy}
      />
      <PdfReader paper={selectedPaper} />
      <section className="rightRail">
        <PaperOverview
          paper={selectedPaper}
          overview={overview}
          loading={overviewLoading}
          error={overviewError}
        />
        {drafts.length ? (
          <div className="draftTabs">
            {drafts.map((draft) => (
              <button
                key={draft.draft_id}
                className={selectedDraft?.draft_id === draft.draft_id ? "active" : ""}
                onClick={() => {
                  setSelectedDraftId(draft.draft_id);
                  setSelectedPaper(draft);
                }}
              >
                {decodeEntities(draft.title || draft.paper_id)}
              </button>
            ))}
          </div>
        ) : null}
        {selectedDraft ? (
          <DraftEditor
            draft={selectedDraft}
            onSave={saveDraft}
            onAccept={acceptDraft}
            onReject={rejectDraft}
          />
        ) : null}
      </section>
      {notice ? <div className="toast">{notice}</div> : null}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
