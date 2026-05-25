import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  Folder,
  FolderInput,
  RefreshCw,
  Save,
  Search,
  Tag,
  Trash2,
  X,
} from "lucide-react";
import * as pdfjsLib from "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.mjs";
import { api, isMoveTargetConflict } from "/src/api.js";
import { buildCategoryTree, splitCategoryPath } from "/src/categoryTree.js";

pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.mjs";

const LIBRARY_LIST_LIMIT = 5000;
const AUTO_REFRESH_INTERVAL_MS = 60000;
const ALL_CATEGORIES = "__all__";
const UNTAGGED_TAG_FILTER = "__untagged__";
const READ_STATUS_LABELS = {
  unread: "未读",
  reading: "在读",
  read: "已读",
};
const READING_FILTER_OPTIONS = [
  ["", "全部阅读"],
  ["favorite", "重点阅读"],
  ["later", "稍后看"],
  ...Object.entries(READ_STATUS_LABELS),
];

const RIGHT_PANEL_TABS = [
  ["overview", "概览", BookOpen],
  ["details", "题录", Save],
  ["organize", "整理", FolderInput],
  ["drafts", "AI", FileText],
  ["quality", "体检", Check],
];

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

function parseTagInput(value) {
  const tags = [];
  const seen = new Set();
  for (const rawTag of String(value || "").split(/[,，\n\r]+/)) {
    const tag = rawTag.replace(/\s+/g, " ").trim();
    if (!tag) continue;
    const key = tag.toLocaleLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
  }
  return tags;
}

function uniqueTagList(values) {
  const tags = [];
  const seen = new Set();
  for (const rawValue of values || []) {
    const tag = decodeEntities(rawValue).replace(/\s+/g, " ").trim();
    if (!tag) continue;
    const key = tag.toLocaleLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    tags.push(tag);
  }
  return tags;
}

function categoryTagCandidates(paper) {
  return uniqueTagList(String(decodeEntities(paper?.category_path || "")).split("/"));
}

function categoryFallbackLabel(categoryPath) {
  const segments = splitCategoryPath(categoryPath);
  return decodeEntities(segments[segments.length - 1] || categoryPath || "根目录");
}

function defaultReadingState() {
  return { read_status: "unread", is_favorite: false, is_later: false };
}

function normalizeReadingState(value) {
  return {
    ...defaultReadingState(),
    ...(value || {}),
  };
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

function CategoryFolderRow({ node, selected, onOpen }) {
  return (
    <button
      type="button"
      className={`folderRow ${selected ? "selected" : ""}`}
      onClick={onOpen}
      title={node.path || "全部分类"}
    >
      <Folder size={17} aria-hidden="true" />
      <span>
        <strong>{categoryFallbackLabel(node.label || node.path)}</strong>
        <small>{node.children.length ? `${node.children.length} 个子文件夹` : "文件夹"}</small>
      </span>
      <em>{node.subtree_count || node.paper_count} 篇</em>
      {node.children.length ? <ChevronRight size={15} aria-hidden="true" /> : null}
    </button>
  );
}

function CategoryBreadcrumb({ currentNode, onBrowse }) {
  const breadcrumbSegments = splitCategoryPath(currentNode.path);

  return (
    <div className="categoryExplorerBar">
      <button
        type="button"
        className="categoryBackButton"
        onClick={() => onBrowse(splitCategoryPath(currentNode.path).slice(0, -1).join("/"))}
        disabled={!currentNode.path}
        aria-label="返回上一级分类"
      >
        <ChevronLeft size={15} />
      </button>
      <div className="categoryBreadcrumb" aria-label="当前分类层级">
        <button type="button" onClick={() => onBrowse("")}>
          全部分类
        </button>
        {breadcrumbSegments.map((segment, index) => (
          <React.Fragment key={`${segment}-${index}`}>
            <span>/</span>
            {index === breadcrumbSegments.length - 1 ? (
              <strong>{decodeEntities(segment)}</strong>
            ) : (
              <button type="button" onClick={() => onBrowse(breadcrumbSegments.slice(0, index + 1).join("/"))}>
                {decodeEntities(segment)}
              </button>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function PaperList({
  papers,
  categories,
  tags,
  query,
  setQuery,
  categoryFilter,
  setCategoryFilter,
  tagFilter,
  setTagFilter,
  readingFilter,
  setReadingFilter,
  onSearch,
  onScan,
  onClearFilters,
  selectedId,
  onSelect,
  busy,
}) {
  const hasFilters = Boolean(query.trim() || categoryFilter !== ALL_CATEGORIES || tagFilter || readingFilter);
  const [categoryBrowsePath, setCategoryBrowsePath] = useState("");
  const categoryTree = useMemo(() => buildCategoryTree(categories), [categories]);
  const currentCategoryNode = categoryTree.nodesByPath.get(categoryBrowsePath) || categoryTree.root;
  const hasListFilter = Boolean(query.trim() || tagFilter || readingFilter);
  const browsingFolders =
    !hasListFilter &&
    categoryFilter === ALL_CATEGORIES &&
    currentCategoryNode.children.length > 0;
  const showPapers = !browsingFolders;
  const libraryTitle = showPapers
    ? currentCategoryNode.path && categoryFilter !== ALL_CATEGORIES
      ? "当前文件夹文献"
      : hasFilters
        ? "筛选结果"
        : "全部文献"
    : currentCategoryNode.path
      ? "内部文件夹"
      : "全部分类";

  useEffect(() => {
    if (categoryFilter !== ALL_CATEGORIES && categoryTree.nodesByPath.has(categoryFilter)) {
      setCategoryBrowsePath(categoryFilter);
    }
  }, [categoryFilter, categoryTree]);

  useEffect(() => {
    if (categoryBrowsePath && !categoryTree.nodesByPath.has(categoryBrowsePath)) {
      setCategoryBrowsePath("");
    }
  }, [categoryBrowsePath, categoryTree]);

  const browseCategoryPath = (categoryPath) => {
    setCategoryBrowsePath(categoryPath);
    setCategoryFilter(ALL_CATEGORIES);
  };

  const openCategoryNode = (node) => {
    setCategoryBrowsePath(node.path);
    setCategoryFilter(node.children.length ? ALL_CATEGORIES : node.path);
  };

  const viewCurrentFolderPapers = () => {
    if (currentCategoryNode.path) setCategoryFilter(currentCategoryNode.path);
  };

  const clearAllFilters = () => {
    setCategoryBrowsePath("");
    onClearFilters();
  };

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
      <div className="filterPanel">
        <select
          value={tagFilter}
          onChange={(event) => setTagFilter(event.target.value)}
          aria-label="按标签筛选"
        >
          <option value="">全部标签</option>
          <option value={UNTAGGED_TAG_FILTER}>未打标签</option>
          {tags.map((tagItem) => (
            <option key={tagItem.tag} value={tagItem.tag}>
              {decodeEntities(tagItem.tag)}
              {Number.isFinite(tagItem.paper_count) ? ` (${tagItem.paper_count})` : ""}
            </option>
          ))}
        </select>
        <select
          value={readingFilter}
          onChange={(event) => setReadingFilter(event.target.value)}
          aria-label="按阅读状态筛选"
        >
          {READING_FILTER_OPTIONS.map(([value, label]) => (
            <option key={value || "__all_reading__"} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button
          onClick={clearAllFilters}
          disabled={!hasFilters}
        >
          <X size={15} />
          清除
        </button>
      </div>
      <CategoryBreadcrumb currentNode={currentCategoryNode} onBrowse={browseCategoryPath} />
      <div className="libraryMeta">
        <strong>{libraryTitle}</strong>
        <span>{showPapers ? `${papers.length} 篇` : `${currentCategoryNode.children.length} 个文件夹`}</span>
      </div>
      <div className="paperList">
        {!showPapers ? (
          <>
            {currentCategoryNode.path && currentCategoryNode.paper_count > 0 ? (
              <button type="button" className="currentFolderRow" onClick={viewCurrentFolderPapers}>
                <FileText size={17} aria-hidden="true" />
                <span>
                  <strong>查看当前文件夹文献</strong>
                  <small>{categoryFallbackLabel(currentCategoryNode.label || currentCategoryNode.path)}</small>
                </span>
                <em>{currentCategoryNode.paper_count} 篇</em>
              </button>
            ) : null}
            {currentCategoryNode.children.map((child) => (
              <CategoryFolderRow
                key={child.path}
                node={child}
                selected={categoryFilter === child.path}
                onOpen={() => openCategoryNode(child)}
              />
            ))}
          </>
        ) : null}
        {showPapers && papers.map((paper) => {
          const title = decodeEntities(paper.title || "Untitled");
          const paperTags = normalizedList(paper.tags);
          const readingState = normalizeReadingState(paper.reading_state);
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
                <span className="readingBadgeList">
                  <span>{READ_STATUS_LABELS[readingState.read_status] || "未读"}</span>
                  {readingState.is_favorite ? <span>重点</span> : null}
                  {readingState.is_later ? <span>稍后看</span> : null}
                </span>
                {paperTags.length ? (
                  <span className="paperTagList">
                    {paperTags.map((paperTag) => (
                      <span key={paperTag}>{paperTag}</span>
                    ))}
                  </span>
                ) : null}
                {paper.snippet ? <em>{decodeEntities(paper.snippet)}</em> : null}
              </span>
            </button>
          );
        })}
        {showPapers && !papers.length ? (
          <div className="emptyState">当前条件下没有文献</div>
        ) : null}
      </div>
    </aside>
  );
}

function PdfReader({ paper }) {
  const canvasRef = useRef(null);
  const renderTaskRef = useRef(null);
  const [pdfDoc, setPdfDoc] = useState(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [viewMode, setViewMode] = useState("pages");
  const [message, setMessage] = useState("");
  const [pdfSearchQuery, setPdfSearchQuery] = useState("");
  const [pdfSearchResults, setPdfSearchResults] = useState([]);
  const [pdfSearchLoading, setPdfSearchLoading] = useState(false);
  const [pdfSearchMessage, setPdfSearchMessage] = useState("");
  const paperId = paper?.paper_id || "";
  const pdfUrl = paperId ? `/local/papers/${paperId}/pdf` : "";

  useEffect(() => {
    let cancelled = false;
    setPdfDoc(null);
    setPageNumber(1);
    setPageCount(0);
    setPdfLoading(Boolean(paperId));
    setMessage("");
    setPdfSearchQuery("");
    setPdfSearchResults([]);
    setPdfSearchMessage("");
    if (canvasRef.current) {
      const canvas = canvasRef.current;
      const context = canvas.getContext("2d");
      context?.clearRect(0, 0, canvas.width, canvas.height);
    }
    if (!paperId) return;

    const task = pdfjsLib.getDocument({ url: pdfUrl });
    task.promise
      .then((doc) => {
        if (cancelled) return;
        setPdfDoc(doc);
        setPageCount(doc.numPages);
      })
      .catch((error) => {
        if (!cancelled) setMessage(`PDF 加载失败：${error.message || "请确认文件还在原位置"}`);
      })
      .finally(() => {
        if (!cancelled) setPdfLoading(false);
      });

    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
      task.destroy?.();
    };
  }, [paperId, pdfUrl]);

  useEffect(() => {
    let cancelled = false;
    async function renderPage() {
      if (!pdfDoc || !canvasRef.current || viewMode !== "pages") return;
      setPdfLoading(true);
      setMessage("");
      renderTaskRef.current?.cancel();
      try {
        const page = await pdfDoc.getPage(pageNumber);
        if (cancelled) return;
        const container = canvasRef.current.parentElement;
        const baseViewport = page.getViewport({ scale: 1 });
        const width = Math.max(320, container.clientWidth - 24);
        const scale = Math.min(1.7, width / baseViewport.width);
        const viewport = page.getViewport({ scale });
        const canvas = canvasRef.current;
        const context = canvas.getContext("2d");
        const outputScale = Math.max(1, window.devicePixelRatio || 1);
        canvas.width = Math.floor(viewport.width * outputScale);
        canvas.height = Math.floor(viewport.height * outputScale);
        canvas.style.width = `${Math.floor(viewport.width)}px`;
        canvas.style.height = `${Math.floor(viewport.height)}px`;
        context.setTransform(outputScale, 0, 0, outputScale, 0, 0);
        const renderTask = page.render({ canvasContext: context, viewport });
        renderTaskRef.current = renderTask;
        await renderTask.promise;
      } catch (error) {
        if (!cancelled && error?.name !== "RenderingCancelledException") {
          setMessage(`PDF 渲染失败：${error.message || "请试试原始 PDF 模式"}`);
        }
      } finally {
        if (!cancelled) setPdfLoading(false);
      }
    }
    renderPage();
    return () => {
      cancelled = true;
      renderTaskRef.current?.cancel();
    };
  }, [pdfDoc, pageNumber, viewMode]);

  const goToPage = (value) => {
    const nextPage = Number(value);
    if (!Number.isFinite(nextPage)) return;
    setPageNumber(Math.min(Math.max(1, nextPage), pageCount || nextPage));
  };

  const searchPdfPages = async () => {
    const query = pdfSearchQuery.trim();
    if (!paper?.paper_id || !query) {
      setPdfSearchResults([]);
      setPdfSearchMessage("");
      return;
    }

    setPdfSearchLoading(true);
    setPdfSearchMessage("");
    try {
      const result = await api(
        `/local/papers/${paper.paper_id}/page-search?query=${encodeURIComponent(query)}`,
      );
      const results = result.results || [];
      setPdfSearchResults(results);
      setPdfSearchMessage(results.length ? `${results.length} 个命中页` : "没有命中页");
      if (results[0]?.page_number) {
        goToPage(results[0].page_number);
      }
    } catch (error) {
      setPdfSearchResults([]);
      setPdfSearchMessage(error.message);
    } finally {
      setPdfSearchLoading(false);
    }
  };

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
        {viewMode === "pages" ? (
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
            <input
              type="number"
              min="1"
              max={pageCount || undefined}
              value={pageNumber}
              onChange={(event) => goToPage(event.target.value)}
              aria-label="跳转页码"
            />
            <span>/ {pageCount || "-"}</span>
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
        ) : (
          <a className="pdfOpenLink" href={pdfUrl} target="_blank" rel="noreferrer">
            新窗口打开
          </a>
        )}
      </div>
      <div className="pdfViewSwitch">
        <button
          type="button"
          className={viewMode === "pages" ? "active" : ""}
          onClick={() => setViewMode("pages")}
        >
          <BookOpen size={16} />
          页面阅读
        </button>
        <button
          type="button"
          className={viewMode === "native" ? "active" : ""}
          onClick={() => setViewMode("native")}
        >
          <FileText size={16} />
          原始 PDF
        </button>
      </div>
      <div className="pdfTools">
        <div className="readerSearchBox">
          <Search size={16} aria-hidden="true" />
          <input
            value={pdfSearchQuery}
            onChange={(event) => setPdfSearchQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") searchPdfPages();
            }}
            placeholder="在当前 PDF 内搜索"
          />
        </div>
        <button onClick={searchPdfPages} disabled={pdfSearchLoading || !pdfSearchQuery.trim()}>
          <Search size={16} />
          搜索
        </button>
      </div>
      {pdfSearchMessage ? <div className="pdfSearchMessage">{pdfSearchMessage}</div> : null}
      {pdfSearchResults.length ? (
        <div className="pdfSearchResults">
          {pdfSearchResults.map((result) => (
            <button key={`${result.page_number}-${result.snippet}`} onClick={() => goToPage(result.page_number)}>
              <strong>P{result.page_number}</strong>
              <span>{decodeEntities(result.snippet)}</span>
            </button>
          ))}
        </div>
      ) : null}
      {pdfLoading ? <div className="pdfStatus">PDF 读取中...</div> : null}
      {message ? <div className="inlineError">{message}</div> : null}
      {viewMode === "native" ? (
        <div className="pdfNativeWrap">
          <iframe className="pdfFrame" src={pdfUrl} title={`${title} PDF`} />
        </div>
      ) : (
        <div className="canvasWrap">
          <canvas ref={canvasRef} />
        </div>
      )}
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
  const overviewZh = decodeEntities(overview?.translated_chinese_overview || "");
  const overviewEn = decodeEntities(overview?.auto_english_overview || abstractEn);
  const summaryZh = decodeEntities(overview?.translated_chinese_abstract || "");
  const summaryZhMissing =
    overview?.missing_chinese_reason || "腾讯云翻译还未生成中文摘要。";
  const overviewZhMissing =
    overview?.missing_chinese_reason || "腾讯云翻译还未生成中文概览。";
  const mainPointsZh = normalizedList(overview?.translated_main_points_zh);
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
                {overviewZh || overviewZhMissing}
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
                  ) : point.en ? (
                    <div className="pointLanguage notice">
                      <strong>中文</strong>
                      <p>腾讯云翻译未生成此要点中文。</p>
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

function MetadataEditor({ paper, onSave, saving }) {
  const [form, setForm] = useState({
    title: "",
    authors: "",
    year: "",
    journal: "",
    doi: "",
  });

  useEffect(() => {
    setForm({
      title: decodeEntities(paper?.title || ""),
      authors: decodeEntities(paper?.authors || ""),
      year: paper?.year ?? "",
      journal: decodeEntities(paper?.journal || ""),
      doi: decodeEntities(paper?.doi || ""),
    });
  }, [paper?.paper_id, paper?.title, paper?.authors, paper?.year, paper?.journal, paper?.doi]);

  if (!paper) return null;

  const setField = (key, value) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const payload = {
    title: form.title.trim(),
    authors: form.authors.trim(),
    year: form.year === "" ? null : Number(form.year),
    journal: form.journal.trim(),
    doi: form.doi.trim(),
  };

  return (
    <section className="metadataPanel">
      <div className="categoryHeader">
        <strong>题录修正</strong>
        <span>{payload.title ? "可保存" : "缺标题"}</span>
      </div>
      <label className="field compactField">
        <span>标题</span>
        <input value={form.title} onChange={(event) => setField("title", event.target.value)} />
      </label>
      <div className="twoCol">
        <label className="field compactField">
          <span>作者</span>
          <input
            value={form.authors}
            onChange={(event) => setField("authors", event.target.value)}
          />
        </label>
        <label className="field compactField">
          <span>年份</span>
          <input
            type="number"
            value={form.year}
            onChange={(event) => setField("year", event.target.value)}
          />
        </label>
      </div>
      <div className="twoCol">
        <label className="field compactField">
          <span>期刊</span>
          <input
            value={form.journal}
            onChange={(event) => setField("journal", event.target.value)}
          />
        </label>
        <label className="field compactField">
          <span>DOI</span>
          <input value={form.doi} onChange={(event) => setField("doi", event.target.value)} />
        </label>
      </div>
      <button className="saveTagButton" onClick={() => onSave(payload)} disabled={saving || !payload.title}>
        <Save size={16} />
        保存题录
      </button>
    </section>
  );
}

function ReadingStateEditor({ paper, readingState, onSave, saving }) {
  const [draft, setDraft] = useState(defaultReadingState());

  useEffect(() => {
    setDraft(normalizeReadingState(readingState || paper?.reading_state));
  }, [paper?.paper_id, readingState, paper?.reading_state]);

  if (!paper) return null;

  const setField = (key, value) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  return (
    <section className="readingPanel">
      <div className="categoryHeader">
        <strong>阅读状态</strong>
        <span>{READ_STATUS_LABELS[draft.read_status] || "未读"}</span>
      </div>
      <div className="segmentedControl">
        {Object.entries(READ_STATUS_LABELS).map(([value, label]) => (
          <button
            key={value}
            className={draft.read_status === value ? "active" : ""}
            onClick={() => setField("read_status", value)}
            type="button"
          >
            {label}
          </button>
        ))}
      </div>
      <div className="checkGrid readingChecks">
        <label className="checkItem">
          <input
            type="checkbox"
            checked={draft.is_favorite}
            onChange={(event) => setField("is_favorite", event.target.checked)}
          />
          <span>重点</span>
        </label>
        <label className="checkItem">
          <input
            type="checkbox"
            checked={draft.is_later}
            onChange={(event) => setField("is_later", event.target.checked)}
          />
          <span>稍后看</span>
        </label>
      </div>
      <button className="saveTagButton" onClick={() => onSave(draft)} disabled={saving}>
        <Save size={16} />
        保存状态
      </button>
    </section>
  );
}

function CategoryMover({ paper, categories, onMove, moving }) {
  const [categoryPath, setCategoryPath] = useState("");

  useEffect(() => {
    setCategoryPath(paper?.category_path || "");
  }, [paper?.paper_id, paper?.category_path]);

  if (!paper) return null;

  const currentCategory = paper.category_path || "";
  const options = categories.some((category) => category.category_path === currentCategory)
    ? categories
    : [
        {
          category_path: currentCategory,
          category_label: currentCategory || "根目录",
          paper_count: 0,
        },
        ...categories,
      ];

  return (
    <section className="categoryPanel">
      <div className="categoryHeader">
        <strong>文件夹分类</strong>
        <span>{decodeEntities(paper.category_label || currentCategory || "根目录")}</span>
      </div>
      <div className="categoryControls">
        <select
          value={categoryPath}
          onChange={(event) => setCategoryPath(event.target.value)}
          aria-label="选择已有分类"
        >
          {options.map((category) => (
            <option key={category.category_path || "__root__"} value={category.category_path}>
              {decodeEntities(category.category_label || category.category_path || "根目录")}
              {Number.isFinite(category.paper_count) ? ` (${category.paper_count})` : ""}
            </option>
          ))}
        </select>
        <input
          value={categoryPath}
          onChange={(event) => setCategoryPath(event.target.value)}
          placeholder="新分类/子分类"
          aria-label="分类路径"
        />
        <button
          onClick={() => onMove(categoryPath)}
          disabled={moving || categoryPath === currentCategory}
        >
          <FolderInput size={17} />
          移动
        </button>
      </div>
    </section>
  );
}

function TagEditor({ paper, tags, tagSuggestions, paperTags, onSave, saving }) {
  const [tagText, setTagText] = useState("");

  useEffect(() => {
    setTagText((paperTags || []).join(", "));
  }, [paper?.paper_id, paperTags]);

  if (!paper) return null;

  const parsedTags = parseTagInput(tagText);
  const parsedKeys = new Set(parsedTags.map((tag) => tag.toLocaleLowerCase()));
  const categorySuggestions = categoryTagCandidates(paper)
    .filter((tag) => !parsedKeys.has(tag.toLocaleLowerCase()))
    .slice(0, 8);
  const categoryKeys = new Set(categoryTagCandidates(paper).map((tag) => tag.toLocaleLowerCase()));
  const globalSuggestions = uniqueTagList([
    ...(tagSuggestions || []).map((tagItem) => tagItem?.tag),
    ...(tags || []).map((tagItem) => tagItem?.tag),
  ])
    .filter(
      (tag) =>
        !parsedKeys.has(tag.toLocaleLowerCase()) && !categoryKeys.has(tag.toLocaleLowerCase()),
    )
    .slice(0, 12);

  const addTag = (tag) => {
    const nextTags = parseTagInput(`${tagText}\n${tag}`);
    setTagText(nextTags.join(", "));
  };

  const renderSuggestionGroup = (title, items) =>
    items.length ? (
      <div className="tagSuggestionBlock">
        <span>{title}</span>
        <div className="tagSuggestions">
          {items.map((tag) => (
            <button key={`${title}-${tag}`} onClick={() => addTag(tag)} type="button">
              <Tag size={13} />
              {tag}
            </button>
          ))}
        </div>
      </div>
    ) : null;

  return (
    <section className="tagPanel">
      <div className="categoryHeader">
        <strong>标签</strong>
        <span>{parsedTags.length ? `${parsedTags.length} 个` : "未设置"}</span>
      </div>
      <textarea
        value={tagText}
        onChange={(event) => setTagText(event.target.value)}
        placeholder="输入标签，用逗号或换行分隔"
        aria-label="文献标签"
        rows={3}
      />
      {renderSuggestionGroup("分类建议", categorySuggestions)}
      {renderSuggestionGroup("全局建议", globalSuggestions)}
      {!categorySuggestions.length && !globalSuggestions.length ? (
        <p className="tagEmptyHint">暂无可用建议，保存标签后会出现在这里。</p>
      ) : null}
      <button className="saveTagButton" onClick={() => onSave(parsedTags)} disabled={saving}>
        <Save size={16} />
        保存标签
      </button>
    </section>
  );
}

function AutoClassifyPanel({
  onRun,
  running,
  result,
  emptyCategories,
  onDeleteEmptyCategory,
  deletingEmptyCategory,
}) {
  const emptyCategoryItems = Array.isArray(emptyCategories) ? emptyCategories : [];
  const resultItems = Array.isArray(result?.results) ? result.results : [];
  const reviewItems = resultItems.filter((item) =>
    ["needs_review", "multi_topic_review", "conflict", "failed"].includes(item.status),
  );
  const moveItems = resultItems.filter((item) =>
    ["move", "planned", "moved"].includes(item.status),
  );
  const duplicateItems = resultItems.filter((item) => item.duplicate || item.type === "duplicate_file");

  const runPreview = () => onRun({ dryRun: true });
  const runApply = () => onRun({ dryRun: false });
  const statusLabel = (status) =>
    status === "conflict"
      ? "同名冲突"
      : status === "failed"
        ? "失败"
        : status === "multi_topic_review"
          ? "多主题待确认"
          : status === "move" || status === "planned"
            ? "可移动"
            : status === "moved"
              ? "已移动"
              : "待确认";
  const strictLabel = (item) =>
    item?.strict
      ? `${item.strict.mechanism || "未识别"} / ${item.strict.material_structure || "未识别"} / ${
          item.strict.application || "基础性能研究"
        }`
      : item?.target_category_path || "";

  return (
    <section className="autoClassifyPanel">
      <div className="autoClassifyActions">
        <button onClick={runPreview} disabled={running} type="button">
          <RefreshCw size={16} className={running ? "spin" : ""} />
          预览严格分类
        </button>
        <button onClick={runApply} disabled={running} type="button">
          <FolderInput size={16} className={running ? "spin" : ""} />
          执行严格分类
        </button>
      </div>
      {result ? (
        <div className="autoClassifyStats">
          <span>
            <strong>{result.filesystem_pdf_count || result.candidates || 0}</strong> PDF
          </span>
          <span>
            <strong>{result.planned || result.moved || 0}</strong> 可移动/已移动
          </span>
          <span>
            <strong>{result.skipped_review || 0}</strong> 待确认
          </span>
          <span>
            <strong>{result.renamed || 0}</strong> 改名
          </span>
          <span>
            <strong>{result.duplicate_file_count || 0}</strong> 重复PDF
          </span>
          <span>
            <strong>{result.conflicts || 0}</strong> 冲突
          </span>
          <span>
            <strong>{result.failed || 0}</strong> 失败
          </span>
        </div>
      ) : null}
      {moveItems.length ? (
        <div className="autoStrictList">
          <div className="categoryHeader">
            <strong>{result?.dry_run ? "严格分类预览" : "严格分类结果"}</strong>
            <span>{moveItems.length} 项</span>
          </div>
          {moveItems.slice(0, 8).map((item) => (
            <div key={`${item.source_path}-${item.status}-${item.target_path}`}>
              <strong>{decodeEntities(item.title || item.source?.name || item.paper_id)}</strong>
              <span>
                {statusLabel(item.status)} · {decodeEntities(strictLabel(item))} · score {item.score ?? "-"}
              </span>
              {item.strict?.chinese_brief_work ? (
                <span>简要内容：{decodeEntities(item.strict.chinese_brief_work)}</span>
              ) : null}
              {item.best_guess ? (
                <span>最佳猜测：{(item.needs_review_reasons || []).join(", ") || "low confidence"}</span>
              ) : null}
              <span>{decodeEntities(item.target_path || item.target?.path || item.target_category_path || "")}</span>
              {item.target_filename ? <span>文件名：{decodeEntities(item.target_filename)}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
      {reviewItems.length ? (
        <div className="autoReviewList">
          {reviewItems.slice(0, 6).map((item) => (
            <div key={`${item.source_path || item.paper_id}-${item.status}-${item.target_category_path}`}>
              <strong>{decodeEntities(item.title || item.source?.name || item.paper_id)}</strong>
              <span>
                {statusLabel(item.status)} · {decodeEntities(strictLabel(item) || "待分类")} · score {item.score ?? "-"}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {duplicateItems.length ? (
        <div className="autoDuplicateList">
          <div className="categoryHeader">
            <strong>重复 PDF 归类</strong>
            <span>{duplicateItems.length} 个</span>
          </div>
          {duplicateItems.slice(0, 6).map((item) => (
            <div key={`${item.source_path}-${item.target_path || item.status}`}>
              <strong>{decodeEntities(item.source?.name || item.title || item.paper_id)}</strong>
              <span>
                {statusLabel(item.status)} · {decodeEntities(item.target_path || item.target?.path || "待确认")}
              </span>
              {item.target_filename ? <span>文件名：{decodeEntities(item.target_filename)}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
      <div className="emptyCategoryPanel">
        <div className="categoryHeader">
          <strong>空文件夹</strong>
          <span>{emptyCategoryItems.length} 个</span>
        </div>
        {emptyCategoryItems.length ? (
          <div className="emptyCategoryList">
            {emptyCategoryItems.map((category) => (
              <div key={category.category_path}>
                <span>{decodeEntities(category.category_path)}</span>
                <button
                  type="button"
                  title="删除空文件夹"
                  aria-label={`删除空文件夹 ${category.category_path}`}
                  onClick={() => onDeleteEmptyCategory(category.category_path)}
                  disabled={deletingEmptyCategory === category.category_path}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="tagEmptyHint">无</p>
        )}
      </div>
    </section>
  );
}

function QualityPanel({ report, loading, onRefresh, onSelectIssue }) {
  const summary = report?.summary || {};
  const items = [
    ["missing_doi", "缺 DOI"],
    ["missing_authors", "缺作者"],
    ["missing_year", "缺年份"],
    ["future_year", "年份异常"],
    ["duplicate_files", "重复文件"],
    ["uncategorized", "未分类"],
    ["missing_tags", "未打标签"],
    ["no_summary", "未生成摘要"],
  ];

  return (
    <section className="qualityPanel">
      <div className="categoryHeader">
        <strong>质量体检</strong>
        <span>{report ? `${report.issue_count} 项` : "未运行"}</span>
      </div>
      <button className="qualityRefresh" onClick={onRefresh} disabled={loading}>
        <RefreshCw size={15} className={loading ? "spin" : ""} />
        刷新体检
      </button>
      {report ? (
        <>
          <div className="qualitySummary">
            {items.map(([key, label]) => (
              <span key={key}>
                {label}
                <strong>{summary[key] || 0}</strong>
              </span>
            ))}
          </div>
          <div className="qualityIssues">
            {(report.issues || []).slice(0, 80).map((issue) => (
              <button
                key={`${issue.kind}-${issue.paper_id}-${issue.detail || ""}`}
                onClick={() => onSelectIssue(issue)}
              >
                <strong>{issue.label}</strong>
                <span>{decodeEntities(issue.title)}</span>
              </button>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}

function ReferenceExportPanel({ onExport, exporting }) {
  const formats = [
    ["bibtex", "BibTeX", ".bib"],
    ["ris", "RIS", ".ris"],
    ["text", "GB/T", ".txt"],
  ];

  return (
    <section className="referenceExportPanel">
      <div className="categoryHeader">
        <strong>参考文献导出</strong>
        <span>{exporting ? "导出中" : "全部/当前列表"}</span>
      </div>
      <div className="referenceExportActions">
        {formats.map(([format, label, extension]) => (
          <button
            key={format}
            type="button"
            onClick={() => onExport(format)}
            disabled={Boolean(exporting)}
            title={`导出 ${label} ${extension}`}
          >
            <Download size={15} className={exporting === format ? "spin" : ""} />
            <span>{label}</span>
          </button>
        ))}
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
  const [categories, setCategories] = useState([]);
  const [tags, setTags] = useState([]);
  const [tagSuggestions, setTagSuggestions] = useState([]);
  const [query, setQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState(ALL_CATEGORIES);
  const [tagFilter, setTagFilter] = useState("");
  const [readingFilter, setReadingFilter] = useState("");
  const [selectedPaper, setSelectedPaper] = useState(null);
  const [selectedPaperTags, setSelectedPaperTags] = useState([]);
  const [selectedReadingState, setSelectedReadingState] = useState(defaultReadingState());
  const [selectedDraftId, setSelectedDraftId] = useState("");
  const [overview, setOverview] = useState(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState("");
  const [busy, setBusy] = useState(false);
  const [autoClassifying, setAutoClassifying] = useState(false);
  const [movingPaper, setMovingPaper] = useState(false);
  const [savingMetadata, setSavingMetadata] = useState(false);
  const [savingReadingState, setSavingReadingState] = useState(false);
  const [savingTags, setSavingTags] = useState(false);
  const [generatingDraft, setGeneratingDraft] = useState(false);
  const [qualityReport, setQualityReport] = useState(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [emptyCategories, setEmptyCategories] = useState([]);
  const [deletingEmptyCategory, setDeletingEmptyCategory] = useState("");
  const [autoClassifyResult, setAutoClassifyResult] = useState(null);
  const [exportingReferences, setExportingReferences] = useState("");
  const [notice, setNotice] = useState("");
  const [activeRightPanel, setActiveRightPanel] = useState("overview");

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
      setSelectedPaperTags(normalizedList(paper.tags));
      setSelectedReadingState(normalizeReadingState(paper.reading_state));
      const draftForPaper = drafts.find((draft) => draft.paper_id === paper.paper_id);
      setSelectedDraftId(draftForPaper?.draft_id || "");
    },
    [drafts],
  );

  const loadPapers = useCallback(async () => {
    const trimmedQuery = query.trim();
    const params = new URLSearchParams({ limit: String(LIBRARY_LIST_LIMIT) });
    if (trimmedQuery) params.set("query", trimmedQuery);
    if (categoryFilter !== ALL_CATEGORIES) params.set("category_path", categoryFilter);
    if (tagFilter) params.set("tag", tagFilter);
    if (readingFilter) params.set("reading_filter", readingFilter);
    const loaded = await api(`/local/papers?${params.toString()}`);
    setPapers(loaded);
    setSelectedPaper((current) => {
      const matching = current
        ? loaded.find((paper) => paper.paper_id === current.paper_id)
        : null;
      if (current && matching) {
        const nextPaper = {
          ...current,
          ...matching,
          category_path: matching.category_path ?? current.category_path ?? "",
          category_label: matching.category_label ?? current.category_label ?? "",
          tags: matching.tags ?? current.tags ?? [],
          reading_state: matching.reading_state ?? current.reading_state ?? defaultReadingState(),
        };
        const unchanged =
          nextPaper.title === current.title &&
          nextPaper.authors === current.authors &&
          nextPaper.year === current.year &&
          nextPaper.journal === current.journal &&
          nextPaper.doi === current.doi &&
          nextPaper.page_count === current.page_count &&
          nextPaper.category_path === current.category_path &&
          nextPaper.category_label === current.category_label &&
          JSON.stringify(nextPaper.tags || []) === JSON.stringify(current.tags || []) &&
          JSON.stringify(nextPaper.reading_state || {}) === JSON.stringify(current.reading_state || {});
        return unchanged ? current : nextPaper;
      }
      return current || loaded[0] || null;
    });
    return loaded;
  }, [categoryFilter, query, readingFilter, tagFilter]);

  const loadDrafts = useCallback(async () => {
    setDrafts(await api("/local/drafts"));
  }, []);

  const loadCategories = useCallback(async () => {
    setCategories(await api("/local/categories"));
  }, []);

  const loadTags = useCallback(async () => {
    setTags(await api("/local/tags"));
  }, []);

  const loadTagSuggestions = useCallback(async () => {
    setTagSuggestions(await api("/local/tag-suggestions?limit=100"));
  }, []);

  const loadQualityReport = useCallback(async () => {
    setQualityLoading(true);
    try {
      setQualityReport(await api("/local/quality-report"));
    } finally {
      setQualityLoading(false);
    }
  }, []);

  const loadEmptyCategories = useCallback(async () => {
    const loaded = await api("/local/empty-categories");
    setEmptyCategories(Array.isArray(loaded) ? loaded : []);
  }, []);

  useEffect(() => {
    loadPapers().catch((error) => setNotice(error.message));
    loadDrafts().catch((error) => setNotice(error.message));
    loadCategories().catch((error) => setNotice(error.message));
    loadTags().catch((error) => setNotice(error.message));
  }, [loadCategories, loadDrafts, loadPapers, loadTags]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      loadPapers().catch((error) => setNotice(error.message));
      loadDrafts().catch((error) => setNotice(error.message));
    }, AUTO_REFRESH_INTERVAL_MS);

    return () => window.clearInterval(intervalId);
  }, [loadDrafts, loadPapers]);

  useEffect(() => {
    if (activeRightPanel === "details") {
      loadTagSuggestions().catch((error) => setNotice(error.message));
    }
    if (activeRightPanel === "organize") {
      loadEmptyCategories().catch((error) => setNotice(error.message));
    }
    if (activeRightPanel === "quality" && !qualityReport) {
      loadQualityReport().catch((error) => setNotice(error.message));
    }
  }, [
    activeRightPanel,
    loadEmptyCategories,
    loadQualityReport,
    loadTagSuggestions,
    qualityReport,
  ]);

  useEffect(() => {
    if (!selectedPaper?.paper_id) {
      setOverview(null);
      setOverviewError("");
      return;
    }
    if (activeRightPanel !== "overview") {
      return;
    }

    let cancelled = false;
    setOverview(null);
    setOverviewLoading(true);
    setOverviewError("");
    api(`/local/papers/${selectedPaper.paper_id}/overview`)
      .then((result) => {
        if (cancelled) return;
        setOverview(result);
        if (result?.reading_state) {
          const loadedReadingState = normalizeReadingState(result.reading_state);
          setSelectedReadingState(loadedReadingState);
          setSelectedPaper((current) =>
            current?.paper_id === result.paper_id
              ? { ...current, reading_state: loadedReadingState }
              : current,
          );
        }
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
  }, [selectedPaper?.paper_id, activeRightPanel]);

  useEffect(() => {
    if (selectedPaper?.paper_id) {
      setSelectedReadingState(normalizeReadingState(selectedPaper.reading_state));
    }
  }, [selectedPaper?.paper_id, selectedPaper?.reading_state]);

  useEffect(() => {
    if (!selectedPaper?.paper_id) {
      setSelectedPaperTags([]);
      setSelectedReadingState(defaultReadingState());
      return;
    }

    let cancelled = false;
    Promise.all([
      api(`/local/papers/${selectedPaper.paper_id}/tags`),
      api(`/local/papers/${selectedPaper.paper_id}/reading-state`),
    ])
      .then((result) => {
        if (cancelled) return;
        const [tagResult, readingResult] = result;
        const loadedTags = normalizedList(tagResult.tags);
        const loadedReadingState = normalizeReadingState(readingResult);
        setSelectedPaperTags(loadedTags);
        setSelectedReadingState(loadedReadingState);
        setSelectedPaper((current) =>
          current?.paper_id === selectedPaper.paper_id
            ? { ...current, tags: loadedTags, reading_state: loadedReadingState }
            : current,
        );
      })
      .catch((error) => {
        if (!cancelled) setNotice(error.message);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedPaper?.paper_id]);

  const clearFilters = () => {
    setQuery("");
    setCategoryFilter(ALL_CATEGORIES);
    setTagFilter("");
    setReadingFilter("");
  };

  const refreshLibraryViews = async () => {
    const refreshedPapers = await loadPapers();
    await loadCategories();
    await loadTags();
    if (activeRightPanel === "details") await loadTagSuggestions();
    if (activeRightPanel === "quality") await loadQualityReport();
    if (activeRightPanel === "organize") await loadEmptyCategories();
    return refreshedPapers;
  };

  const refreshedListNotice = (refreshedPapers) => {
    const visibleCount = Array.isArray(refreshedPapers) ? refreshedPapers.length : papers.length;
    const scope =
      query.trim() || categoryFilter !== ALL_CATEGORIES || tagFilter || readingFilter
        ? "当前筛选列表"
        : "论文列表";
    return `网页已刷新，${scope}显示 ${visibleCount} 篇`;
  };

  const scan = async () => {
    setBusy(true);
    setNotice("正在快速扫描导入，自动分类会在后台继续...");
    try {
      const result = await api("/local/scan?fast=true", { method: "POST" });
      const autoClassified = result.auto_classified ?? result.classification?.moved ?? 0;
      const autoClassifyFailed = result.auto_classify_failed ?? result.classification?.failed ?? 0;
      const autoClassifyPending = result.auto_classify_pending ?? 0;
      const refreshedPapers = await refreshLibraryViews();
      setNotice(
        `导入完成：扫描 ${result.scanned ?? 0}，入库 ${result.indexed ?? 0}，跳过 ${
          result.skipped || 0
        }，失败 ${result.failed ?? 0}，自动分类 ${autoClassified}${
          autoClassifyFailed ? `，分类失败 ${autoClassifyFailed}` : ""
        }${autoClassifyPending ? `，后台识别 ${autoClassifyPending} 篇` : ""}；${refreshedListNotice(refreshedPapers)}`,
      );
      if (autoClassifyPending) {
        window.setTimeout(() => {
          refreshLibraryViews().catch((error) => setNotice(error.message));
        }, 2500);
      }
    } catch (error) {
      setNotice(error.message);
    } finally {
      setBusy(false);
    }
  };

  const autoClassifyPapers = async ({ dryRun = false } = {}) => {
    setAutoClassifying(true);
    setNotice(dryRun ? "正在预览严格分类，请稍候..." : "正在执行严格分类并刷新网页，请稍候...");
    try {
      const result = await api("/local/auto-classify", {
        method: "POST",
        body: JSON.stringify({
          dry_run: dryRun,
          include_classified: true,
          include_duplicates: true,
          min_auto_score: 8,
          strict: true,
          hierarchy_order: "mechanism/material_structure/application",
          target_prefix: "",
          review_policy: "best_guess",
          duplicate_policy: "duplicate_zone",
          rename_policy: "chinese_brief_work",
        }),
      });
      setAutoClassifyResult(result);
      let refreshedPapers = null;
      if (!dryRun) {
        refreshedPapers = await refreshLibraryViews();
      }
      const actionCount = dryRun ? result.planned || 0 : result.moved || result.planned || 0;
      setNotice(
        `${dryRun ? "严格分类预览完成" : "严格分类完成"}：${dryRun ? "可移动" : "已移动"} ${actionCount}，待确认 ${
          result.skipped_review || 0
        }，改名 ${result.renamed || 0}，重复 PDF ${result.duplicate_file_count || 0}，冲突 ${
          result.conflicts || 0
        }，失败 ${result.failed || 0}${dryRun ? "" : `；${refreshedListNotice(refreshedPapers)}`}`,
      );
    } catch (error) {
      setNotice(error.message);
    } finally {
      setAutoClassifying(false);
    }
  };

  const exportReferences = async (format) => {
    setExportingReferences(format);
    setNotice("");
    try {
      const params = new URLSearchParams({ format });
      if (categoryFilter !== ALL_CATEGORIES) {
        params.set("category_path", categoryFilter);
      }
      if (tagFilter) {
        params.set("tag", tagFilter);
      }
      if (readingFilter) {
        params.set("reading_filter", readingFilter);
      }
      if (query.trim() || categoryFilter !== ALL_CATEGORIES || tagFilter || readingFilter) {
        const visibleIds = papers.map((paper) => paper.paper_id).filter(Boolean);
        if (visibleIds.length) {
          params.set("paper_ids", visibleIds.join(","));
        }
      }
      const response = await fetch(`/local/references/export?${params.toString()}`);
      if (!response.ok) {
        const text = await response.text();
        let message = text || "导出失败";
        try {
          message = JSON.parse(text).detail || message;
        } catch {
          // Keep the plain text response.
        }
        throw new Error(message);
      }
      const blob = await response.blob();
      const extension = format === "bibtex" ? "bib" : format === "text" ? "txt" : format;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `references.${extension}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setNotice(`已导出参考文献 ${extension.toUpperCase()} 文件`);
    } catch (error) {
      setNotice(error.message);
    } finally {
      setExportingReferences("");
    }
  };

  const deleteEmptyCategory = async (categoryPath) => {
    if (!categoryPath) return;
    const confirmed = window.confirm(`删除空文件夹「${categoryPath}」？`);
    if (!confirmed) return;

    setDeletingEmptyCategory(categoryPath);
    setNotice("");
    try {
      await api("/local/empty-categories/delete", {
        method: "POST",
        body: JSON.stringify({ category_path: categoryPath }),
      });
      await loadCategories();
      await loadEmptyCategories();
      setNotice("已删除空文件夹");
    } catch (error) {
      setNotice(error.message);
    } finally {
      setDeletingEmptyCategory("");
    }
  };

  const movePaper = async (categoryPath, { overwrite = false } = {}) => {
    if (!selectedPaper?.paper_id) return;

    setMovingPaper(true);
    setNotice("");
    try {
      const result = await api(`/local/papers/${selectedPaper.paper_id}/move`, {
        method: "POST",
        body: JSON.stringify({
          category_path: categoryPath,
          create_missing_category: true,
          overwrite_existing: overwrite,
        }),
      });
      setSelectedPaper((current) =>
        current?.paper_id === selectedPaper.paper_id
          ? {
              ...current,
              category_path: result.category_path,
              category_label: result.category_label,
            }
          : current,
      );
      setOverview((current) =>
        current?.paper_id === selectedPaper.paper_id
          ? {
              ...current,
              category_path: result.category_path,
              category_label: result.category_label,
            }
          : current,
      );
      await loadPapers();
      await loadCategories();
      await loadTags();
      if (activeRightPanel === "details") await loadTagSuggestions();
      if (activeRightPanel === "organize") await loadEmptyCategories();
      setNotice(
        result.status === "overwritten"
          ? "已覆盖同名文件并移动"
          : result.status === "unchanged"
            ? "已在该分类中"
            : "已移动到分类",
      );
    } catch (error) {
      if (!overwrite && isMoveTargetConflict(error)) {
        const confirmed = window.confirm("目标分类里已有同名 PDF，是否覆盖？覆盖后目标同名文件会被替换。");
        if (confirmed) {
          await movePaper(categoryPath, { overwrite: true });
        } else {
          setNotice("已取消覆盖");
        }
        return;
      }
      setNotice(error.message);
    } finally {
      setMovingPaper(false);
    }
  };

  const savePaperMetadata = async (metadata) => {
    if (!selectedPaper?.paper_id) return;

    setSavingMetadata(true);
    setNotice("");
    try {
      const result = await api(`/local/papers/${selectedPaper.paper_id}/metadata`, {
        method: "PUT",
        body: JSON.stringify(metadata),
      });
      setSelectedPaper((current) =>
        current?.paper_id === selectedPaper.paper_id ? { ...current, ...result } : current,
      );
      setOverview((current) =>
        current?.paper_id === selectedPaper.paper_id ? { ...current, ...result } : current,
      );
      await loadPapers();
      if (activeRightPanel === "quality") await loadQualityReport();
      setNotice("已保存题录");
    } catch (error) {
      setNotice(error.message);
    } finally {
      setSavingMetadata(false);
    }
  };

  const saveReadingState = async (readingState) => {
    if (!selectedPaper?.paper_id) return;

    setSavingReadingState(true);
    setNotice("");
    try {
      const result = await api(`/local/papers/${selectedPaper.paper_id}/reading-state`, {
        method: "PUT",
        body: JSON.stringify(readingState),
      });
      const savedReadingState = normalizeReadingState(result);
      setSelectedReadingState(savedReadingState);
      setSelectedPaper((current) =>
        current?.paper_id === selectedPaper.paper_id
          ? { ...current, reading_state: savedReadingState }
          : current,
      );
      setOverview((current) =>
        current?.paper_id === selectedPaper.paper_id
          ? { ...current, reading_state: savedReadingState }
          : current,
      );
      await loadPapers();
      setNotice("已保存阅读状态");
    } catch (error) {
      setNotice(error.message);
    } finally {
      setSavingReadingState(false);
    }
  };

  const savePaperTags = async (rawTags) => {
    if (!selectedPaper?.paper_id) return;

    setSavingTags(true);
    setNotice("");
    try {
      const result = await api(`/local/papers/${selectedPaper.paper_id}/tags`, {
        method: "PUT",
        body: JSON.stringify({ tags: rawTags }),
      });
      const savedTags = normalizedList(result.tags);
      setSelectedPaperTags(savedTags);
      setSelectedPaper((current) =>
        current?.paper_id === selectedPaper.paper_id ? { ...current, tags: savedTags } : current,
      );
      setOverview((current) =>
        current?.paper_id === selectedPaper.paper_id ? { ...current, tags: savedTags } : current,
      );
      await loadTags();
      if (activeRightPanel === "details") await loadTagSuggestions();
      await loadPapers();
      if (activeRightPanel === "quality") await loadQualityReport();
      setNotice("已保存标签");
    } catch (error) {
      setNotice(error.message);
    } finally {
      setSavingTags(false);
    }
  };

  const generateDraft = async () => {
    if (!selectedPaper?.paper_id) return;

    setGeneratingDraft(true);
    setNotice("");
    try {
      const result = await api(`/local/papers/${selectedPaper.paper_id}/drafts/generate`, {
        method: "POST",
      });
      await loadDrafts();
      if (activeRightPanel === "quality") await loadQualityReport();
      setSelectedDraftId(result.draft_id);
      setNotice("已生成待审草稿");
    } catch (error) {
      setNotice(error.message);
    } finally {
      setGeneratingDraft(false);
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
    await loadTags();
    if (activeRightPanel === "details") await loadTagSuggestions();
    await loadPapers();
    if (activeRightPanel === "quality") await loadQualityReport();
    setNotice("已接受");
  };

  const rejectDraft = async (draftId) => {
    await api(`/local/drafts/${draftId}/reject`, { method: "POST" });
    await loadDrafts();
    setNotice("已拒绝");
  };

  const selectQualityIssue = async (issue) => {
    const existingPaper = papers.find((paper) => paper.paper_id === issue.paper_id);
    if (existingPaper) {
      selectPaper(existingPaper);
      return;
    }
    try {
      selectPaper(await api(`/local/papers/${issue.paper_id}`));
    } catch (error) {
      setNotice(error.message);
    }
  };

  return (
    <div className="appShell">
      <PaperList
        papers={papers}
        categories={categories}
        tags={tags}
        query={query}
        setQuery={setQuery}
        categoryFilter={categoryFilter}
        setCategoryFilter={setCategoryFilter}
        tagFilter={tagFilter}
        setTagFilter={setTagFilter}
        readingFilter={readingFilter}
        setReadingFilter={setReadingFilter}
        onSearch={() => loadPapers().catch((error) => setNotice(error.message))}
        onScan={scan}
        onClearFilters={clearFilters}
        selectedId={selectedPaper?.paper_id}
        onSelect={selectPaper}
        busy={busy}
      />
      <PdfReader paper={selectedPaper} />
      <section className="rightRail">
        <div className="rightRailTabs" role="tablist" aria-label="功能面板">
          {RIGHT_PANEL_TABS.map(([id, label, Icon]) => (
            <button
              key={id}
              type="button"
              className={activeRightPanel === id ? "active" : ""}
              onClick={() => setActiveRightPanel(id)}
              aria-selected={activeRightPanel === id}
            >
              <Icon size={15} />
              <span>{label}</span>
            </button>
          ))}
        </div>
        <div className="rightRailContent">
          {activeRightPanel === "overview" ? (
            <PaperOverview
              paper={selectedPaper}
              overview={overview}
              loading={overviewLoading}
              error={overviewError}
            />
          ) : null}

          {activeRightPanel === "details" ? (
            <>
              <MetadataEditor
                paper={overview || selectedPaper}
                onSave={savePaperMetadata}
                saving={savingMetadata}
              />
              <ReadingStateEditor
                paper={overview || selectedPaper}
                readingState={selectedReadingState}
                onSave={saveReadingState}
                saving={savingReadingState}
              />
              <TagEditor
                paper={overview || selectedPaper}
                tags={tags}
                tagSuggestions={tagSuggestions}
                paperTags={selectedPaperTags}
                onSave={savePaperTags}
                saving={savingTags}
              />
              <ReferenceExportPanel
                onExport={exportReferences}
                exporting={exportingReferences}
              />
            </>
          ) : null}

          {activeRightPanel === "organize" ? (
            <>
              <CategoryMover
                paper={overview || selectedPaper}
                categories={categories}
                onMove={movePaper}
                moving={movingPaper}
              />
              <AutoClassifyPanel
                onRun={autoClassifyPapers}
                running={autoClassifying}
                result={autoClassifyResult}
                emptyCategories={emptyCategories}
                onDeleteEmptyCategory={deleteEmptyCategory}
                deletingEmptyCategory={deletingEmptyCategory}
              />
            </>
          ) : null}

          {activeRightPanel === "drafts" ? (
            <>
              <section className="draftGeneratePanel">
                <button onClick={generateDraft} disabled={!selectedPaper || generatingDraft}>
                  <FileText size={16} />
                  生成 AI 草稿
                </button>
              </section>
              {drafts.length ? (
                <div className="draftTabs">
                  {drafts.map((draft) => (
                    <button
                      key={draft.draft_id}
                      className={selectedDraft?.draft_id === draft.draft_id ? "active" : ""}
                      onClick={() => {
                        setSelectedDraftId(draft.draft_id);
                        selectPaper(
                          papers.find((paper) => paper.paper_id === draft.paper_id) || draft,
                        );
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
            </>
          ) : null}

          {activeRightPanel === "quality" ? (
            <QualityPanel
              report={qualityReport}
              loading={qualityLoading}
              onRefresh={() => loadQualityReport().catch((error) => setNotice(error.message))}
              onSelectIssue={selectQualityIssue}
            />
          ) : null}
        </div>
      </section>
      {notice ? (
        <div className="toast" role="status" aria-live="polite">
          {notice}
        </div>
      ) : null}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
