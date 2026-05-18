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

const LIBRARY_LIST_LIMIT = 1000;

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
      <div className="paperList">
        {papers.map((paper) => (
          <button
            className={`paperRow ${selectedId === paper.paper_id ? "selected" : ""}`}
            key={`${paper.paper_id}-${paper.chunk_id || "paper"}`}
            onClick={() => onSelect(paper)}
          >
            <FileText size={17} aria-hidden="true" />
            <span>
              <strong>{paper.title || "Untitled"}</strong>
              <small>
                {[paper.authors, paper.year, paper.doi].filter(Boolean).join(" · ")}
              </small>
              {paper.snippet ? <em>{paper.snippet}</em> : null}
            </span>
          </button>
        ))}
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

  return (
    <main className="reader">
      <div className="readerHeader">
        <div>
          <h1>{paper.title || "Untitled"}</h1>
          <p>{[paper.authors, paper.year, paper.doi].filter(Boolean).join(" · ")}</p>
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
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const selectedDraft = useMemo(
    () => drafts.find((draft) => draft.draft_id === selectedDraftId) || drafts[0] || null,
    [drafts, selectedDraftId],
  );

  const loadPapers = useCallback(async () => {
    const trimmedQuery = query.trim();
    const params = trimmedQuery
      ? `?query=${encodeURIComponent(trimmedQuery)}`
      : `?limit=${LIBRARY_LIST_LIMIT}`;
    setPapers(await api(`/local/papers${params}`));
  }, [query]);

  const loadDrafts = useCallback(async () => {
    setDrafts(await api("/local/drafts"));
  }, []);

  useEffect(() => {
    loadPapers().catch((error) => setNotice(error.message));
    loadDrafts().catch((error) => setNotice(error.message));
  }, []);

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
        onSelect={setSelectedPaper}
        busy={busy}
      />
      <PdfReader paper={selectedPaper} />
      <section className="rightRail">
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
              {draft.title || draft.paper_id}
            </button>
          ))}
        </div>
        <DraftEditor
          draft={selectedDraft}
          onSave={saveDraft}
          onAccept={acceptDraft}
          onReject={rejectDraft}
        />
      </section>
      {notice ? <div className="toast">{notice}</div> : null}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
