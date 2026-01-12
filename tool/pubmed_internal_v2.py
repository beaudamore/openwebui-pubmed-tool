"""
title: PubMed Deep Research Tool v2
author: Beau D'Amore www.damore.ai
version: 2.0.0
description: Deep PubMed research with native OpenWebUI integrations.
requirements: pandas, spacy, nltk
"""

import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from requests import exceptions as requests_exceptions
from tempfile import SpooledTemporaryFile
from typing import Any, Dict, List, Optional, Set, Tuple

import nltk
import pandas as pd
import spacy
from fastapi import Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from pydantic import BaseModel, Field

from open_webui.models.knowledge import Knowledges, KnowledgeUserModel
from open_webui.models.users import Users
from open_webui.routers.files import upload_file_handler
from open_webui.routers.retrieval import (
    ProcessFileForm,
    QueryCollectionsForm,
    process_file,
    query_collection_handler,
)

# Download NLTK data if not present
nltk.download("punkt")
nltk.download("punkt_tab")
nltk.download("stopwords")

# Load spaCy model (download if not present)
try:
    nlp = spacy.load("en_core_web_sm")
except OSError:
    from spacy.cli import download
    download("en_core_web_sm")
    nlp = spacy.load("en_core_web_sm")


class EventEmitter:
    """Centralized emitter providing consistent phases and formatting."""

    def __init__(self, event_emitter=None):
        self.event_emitter = event_emitter

    async def progress_update(self, description):
        await self.emit(description)

    async def error_update(self, description):
        await self.emit(description, "error", True)

    async def success_update(self, description):
        await self.emit(description, "success", True)

    async def emit(self, description="Unknown State", status="in_progress", done=False):
        if self.event_emitter:
            await self.event_emitter(
                {
                    "type": "status",
                    "data": {
                        "status": status,
                        "description": description,
                        "done": done,
                    },
                }
            )


class Tools:
    class Valves(BaseModel):
        model_config = {"arbitrary_types_allowed": True}
        
        pubmed_base_url: str = Field(
            default="https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
            description="PubMed E-utilities base URL",
        )
        pubmed_api_key: str = Field(
            default="",
            description="Optional NCBI API key for higher limits",
        )
        default_knowledge_base: str = Field(
            default="Pubmed Knowledge Base",
            description="Default knowledge base name or ID to search/store data. Will be created automatically if it doesn't exist.",
        )        
        enable_query_variation: bool = Field(
            default=True,
            description="Automatically try alternative search queries if no new results are found. Uses PubMed spell check and intelligent query expansion.",
        )
        fetch_multiplier: float = Field(
            default=2.5,
            description="Multiplier for initial PubMed fetch to account for duplicates. Higher values increase chance of finding new articles but use more API calls. Set to 1.0 to disable optimization.",
        )
        max_query_attempts: int = Field(
            default=3,
            description="Maximum number of query variations to try (1-5). Higher values increase chance of finding results but use more API calls.",
        )
        enable_hybrid_search: bool = Field(
            default=False,
            description="Enable hybrid search (combines semantic and keyword search). Note: Hybrid search must be enabled globally in OpenWebUI settings for this to work. If global setting is OFF, this valve has no effect. If global setting is ON, setting this to False will disable hybrid search for this tool only.",
        )
        hybrid_bm25_weight: float = Field(
            default=0.5,
            description="BM25 weight for hybrid search (0.0-1.0). Higher values favor keyword matching, lower values favor semantic similarity. Only applies when hybrid search is enabled. Default: 0.5 (balanced).",
        )
        enable_enriched_hybrid_search: bool = Field(
            default=False,
            description="Enrich BM25 text for hybrid search with document metadata (filename, title, headings, source). Improves keyword matching by adding context. Only applies when hybrid search is enabled. Note: Global 'Enable Enriched Hybrid Search Texts' setting in OpenWebUI Admin → Settings → Documents takes precedence if set.",
        )
        max_results: int = Field(
            default=10,
            description="Maximum number of NEW results to retrieve per search (excludes articles already in knowledge base)",
        )
        reranker_results: int = Field(
            default=0,
            description="Number of results to retain after reranking (0 disables reranking)",
        )
        relevance_threshold: float = Field(
            default=0.0,
            description="Minimum relevance score threshold for results (0.0-1.0)",
        )
        enable_debug_output: bool = Field(
            default=True,
            description="Include debug information in responses",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def deep_research_pubmed(
        self,
        query: str,
        __event_emitter__=None,
        __user__=None,
        __request__: Optional[Any] = None,
    ) -> str:
        """
        Search PubMed for medical literature with intelligent knowledge base management.

        Searches PubMed's database and stores results in your knowledge base. Automatically:
        - Filters out articles you already have (deduplication by PMID)
        - Tries alternative queries if no new results found (spell check & query broadening)
        - Extracts entities, keywords, and structured data with NLP
        - Provides comprehensive article details (abstract, authors, DOI, references, figures)
        - Integrates with OpenWebUI's RAG for follow-up questions

        Args:
            query (str): Medical research query. Examples:
                - "SGLT2 inhibitors heart failure"
                - "COVID-19 long term neurological effects"
                - "breast cancer immunotherapy 2024"
                - "diabetes treatment guidelines"

        Returns:
            str: Formatted results including:
                - Existing knowledge base records (if any)
                - New articles found and archived
                - Full article details with abstracts, keywords, entities
                - Progress information (duplicates filtered, query variations tried)

        Examples:
            >>> deep_research_pubmed("diabetes treatment")
            Returns new articles about diabetes treatment, filtering any already stored

            >>> deep_research_pubmed("diabeetes complications")
            Auto-corrects to "diabetes complications" and returns results
        """

        async def _noop_event(*_args, **_kwargs):
            return None

        eventer = __event_emitter__ if __event_emitter__ is not None else _noop_event

        emitter = EventEmitter(eventer)
        await emitter.progress_update("🔬 Initializing PubMed deep research...")

        effective_max_results = self.valves.max_results
        settings_info = f"Max Results: {effective_max_results}"
        if self.valves.reranker_results > 0:
            settings_info += f", Reranker: {self.valves.reranker_results}"
        if self.valves.relevance_threshold > 0.0:
            settings_info += f", Relevance ≥ {self.valves.relevance_threshold}"
        await emitter.progress_update(f"⚙️ Settings: {settings_info}")

        try:
            request = KnowledgeRepository.require_request(__request__)
            user = await KnowledgeRepository.resolve_user(__user__)
        except ValueError as exc:
            msg = f"❌ {exc}"
            await emitter.error_update(msg)
            return msg

        if not self.valves.default_knowledge_base:
            msg = "❌ No default knowledge base configured. Set one in the tool configuration."
            await emitter.error_update(msg)
            return msg

        pubmed_debug = ""
        if self.valves.enable_debug_output:
            pubmed_debug = f"""🔧 **Debug Information**:
- Query: '{query}'
- Knowledge Base: '{self.valves.default_knowledge_base}'
- Max Results (k): {self.valves.max_results}
- Reranker Results (k_reranker): {self.valves.reranker_results}
- Relevance Threshold (r): {self.valves.relevance_threshold}
- Hybrid Search: {self.valves.enable_hybrid_search}

"""

        try:
            kb = await KnowledgeRepository.find_by_name(
                user.id,
                self.valves.default_knowledge_base,
                permission="write",
            )
            if not kb:
                # Create knowledge base if it doesn't exist
                await emitter.progress_update(
                    f"📦 Creating new knowledge base: {self.valves.default_knowledge_base}"
                )
                kb = await KnowledgeRepository.create_knowledge_base(
                    user_id=user.id,
                    name=self.valves.default_knowledge_base,
                    description="Auto-created by PubMed Deep Research Tool",
                )
                await emitter.progress_update(f"✅ Knowledge base created: {kb.name or kb.id}")

            await emitter.progress_update(f"🔍 Querying knowledge container: {kb.name or kb.id}")

            # Get existing PMIDs from file metadata (more reliable than text search)
            existing_pmids = await KnowledgeRepository.get_existing_pmids_from_kb(kb.id)
            
            # Also query for RAG context to show existing results
            rag_result = await KnowledgeRepository.query_knowledge_base(
                request=request,
                user=user,
                kb_id=kb.id,
                query=query,
                limit=self.valves.max_results,
                valves=self.valves,
            )
            documents = rag_result.get("documents", [])
            existing_text = ""
            if documents and documents[0]:
                doc_list = documents[0]
                existing_text = "\n\n".join(doc_list)
                # Supplement with any PMIDs found in text (backup)
                text_pmids = set(re.findall(r"PMID:\s*(\d+)", existing_text))
                existing_pmids.update(text_pmids)
                await emitter.progress_update(f"📚 Found {len(existing_pmids)} existing articles")
            else:
                await emitter.progress_update("ℹ️ No existing knowledge found, will create new entries")

            await emitter.progress_update("🔍 Checking PubMed for new articles...")
            
            # Calculate fetch limit to account for duplicates
            fetch_limit = effective_max_results
            if existing_pmids and self.valves.fetch_multiplier > 1.0:
                fetch_limit = int(effective_max_results * self.valves.fetch_multiplier)
                await emitter.progress_update(
                    f"📥 Fetching up to {fetch_limit} articles to find {effective_max_results} new ones..."
                )
            
            # Track queries tried (for variation logic)
            queries_tried = [query]
            current_query = query
            query_attempt = 1

            def pubmed_search(search_query: str, limit: int = 10) -> List[Dict[str, Any]]:
                esearch_params = {
                    "db": "pubmed",
                    "term": search_query,
                    "retmax": limit,
                    "retmode": "xml",
                    "api_key": self.valves.pubmed_api_key or None,
                }
                esearch_url = f"{self.valves.pubmed_base_url}/esearch.fcgi"
                response = requests.get(esearch_url, params=esearch_params, timeout=10)
                if response.status_code != 200:
                    raise ValueError(f"ESearch failed: {response.text}")

                root = ET.fromstring(response.content)
                pmids = [
                    id_node.text
                    for id_node in root.findall(".//IdList/Id")
                    if id_node.text is not None
                ]
                if not pmids:
                    return []

                esummary_params = {
                    "db": "pubmed",
                    "id": ",".join(pmids),
                    "retmode": "xml",
                    "api_key": self.valves.pubmed_api_key or None,
                }
                esummary_url = f"{self.valves.pubmed_base_url}/esummary.fcgi"
                response = requests.get(esummary_url, params=esummary_params, timeout=10)
                root = ET.fromstring(response.content)

                results: List[Dict[str, Any]] = []
                for docsum in root.findall(".//DocSum"):
                    id_elem = docsum.find("Id")
                    pmid = id_elem.text if id_elem is not None else ""
                    title_elem = docsum.find('.//Item[@Name="Title"]')
                    title = title_elem.text if title_elem is not None else ""
                    author_list = docsum.find('.//Item[@Name="AuthorList"]')
                    authors = (
                        ", ".join(
                            [
                                author.text
                                for author in docsum.findall(
                                    './/Item[@Name="AuthorList"]/Item'
                                )
                                if author.text is not None
                            ]
                        )
                        if author_list is not None
                        else ""
                    )
                    doi_elem = docsum.find('.//Item[@Name="DOI"]')
                    doi = doi_elem.text if doi_elem is not None else ""

                    efetch_params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
                    fetch_response = requests.get(
                        f"{self.valves.pubmed_base_url}/efetch.fcgi",
                        params=efetch_params,
                        timeout=10,
                    )
                    medline_details: Dict[str, Any] = {}
                    if fetch_response.status_code == 200:
                        medline_details = KnowledgeRepository.parse_medline_article(
                            fetch_response.content
                        )
                    abstract = medline_details.get("abstract", "")
                    figures = KnowledgeRepository.fetch_pmc_figures(
                        medline_details.get("pmcid"), self.valves
                    )

                    results.append(
                        {
                            "pmid": pmid,
                            "title": title,
                            "authors": authors,
                            "abstract": abstract,
                            "doi": doi,
                            "abstract_sections": medline_details.get(
                                "abstract_sections", []
                            ),
                            "keywords": medline_details.get("keywords", []),
                            "conflict_of_interest": medline_details.get(
                                "conflict_of_interest", ""
                            ),
                            "references": medline_details.get("references", []),
                            "figures": figures,
                            "pmcid": medline_details.get("pmcid"),
                            "article_ids": medline_details.get("article_ids", []),
                            "journal": medline_details.get("journal", ""),
                            "publication_types": medline_details.get(
                                "publication_types", []
                            ),
                        }
                    )

                return results

            # Retry loop with query variations
            articles = []
            max_attempts = min(max(1, self.valves.max_query_attempts), 5) if self.valves.enable_query_variation else 1
            
            while query_attempt <= max_attempts:
                articles = pubmed_search(current_query, fetch_limit)
                
                # Filter out existing PMIDs and limit to max_results
                if existing_pmids:
                    articles_before_filter = len(articles)
                    articles = [art for art in articles if str(art.get("pmid", "")).strip() not in existing_pmids]
                    filtered_count = articles_before_filter - len(articles)
                    if filtered_count > 0:
                        await emitter.progress_update(
                            f"🔄 Filtered out {filtered_count} existing article(s), {len(articles)} new found"
                        )
                    # Limit to max_results new articles
                    if len(articles) > effective_max_results:
                        articles = articles[:effective_max_results]
                        await emitter.progress_update(f"✂️ Limited to {effective_max_results} new articles")
                
                # If we found new articles, break out of retry loop
                if articles:
                    break
                
                # If no articles and we can try variations
                if query_attempt < max_attempts and self.valves.enable_query_variation:
                    await emitter.progress_update(
                        f"💭 No new results with query '{current_query}', trying variation {query_attempt + 1}..."
                    )
                    
                    # Try PubMed spell check first
                    if query_attempt == 1:
                        spell_suggestion = KnowledgeRepository.get_pubmed_spell_suggestion(current_query, self.valves)
                        if spell_suggestion and spell_suggestion.lower() not in [q.lower() for q in queries_tried]:
                            current_query = spell_suggestion
                            queries_tried.append(current_query)
                            await emitter.progress_update(f"📝 Trying spell-corrected query: '{current_query}'")
                            query_attempt += 1
                            continue
                    
                    # Generate and try variations
                    variations = KnowledgeRepository.generate_query_variations(query)
                    # Filter out already tried queries
                    untried_variations = [
                        v for v in variations 
                        if v.lower() not in [q.lower() for q in queries_tried]
                    ]
                    
                    if untried_variations:
                        current_query = untried_variations[0]
                        queries_tried.append(current_query)
                        await emitter.progress_update(f"🔀 Trying broadened query: '{current_query}'")
                        query_attempt += 1
                    else:
                        # No more variations to try
                        break
                else:
                    # No variations enabled or max attempts reached
                    break
            
            if not articles:
                await emitter.error_update("❌ No NEW articles found in PubMed")
                no_results_msg = "No NEW articles found" if existing_pmids else "No articles found"
                no_results = (
                    f"❌ **PubMed Research**: {no_results_msg} for query '{query}'. "
                    + ("All fetched articles are already in the knowledge base." if existing_pmids else "")
                )
                await eventer(
                    {
                        "type": "result",
                        "data": {
                            "description": no_results,
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
                return pubmed_debug + no_results

            await emitter.progress_update(f"📊 Processing {len(articles)} articles with NLP...")

            def process_data(articles_input: List[Dict[str, str]]) -> pd.DataFrame:
                df = pd.DataFrame(articles_input)

                def clean_text(text):
                    if not text:
                        return ""
                    text = re.sub(r"\s+", " ", text)
                    text = re.sub(r"[^\w\s]", "", text)
                    return text.lower()

                df["clean_title"] = df["title"].apply(clean_text)
                df["clean_abstract"] = df["abstract"].apply(clean_text)

                stop_words = set(stopwords.words("english"))

                def tokenize_and_filter(text):
                    tokens = word_tokenize(text)
                    return [word for word in tokens if word not in stop_words]

                df["tokens_title"] = df["clean_title"].apply(tokenize_and_filter)
                df["tokens_abstract"] = df["clean_abstract"].apply(tokenize_and_filter)

                def spacy_process(text):
                    doc = nlp(text)
                    lemmas = [
                        token.lemma_
                        for token in doc
                        if not token.is_stop and token.is_alpha
                    ]
                    entities = [ent.text for ent in doc.ents]
                    entities = KnowledgeRepository.dedupe_preserve_order(entities)
                    return " ".join(lemmas), entities

                df["lemmas_abstract"], df["entities_abstract"] = zip(
                    *df["clean_abstract"].apply(spacy_process)
                )

                return df

            df = process_data(articles)

            def safe_value(value) -> str:
                if value is None:
                    return "N/A"
                if isinstance(value, float) and pd.isna(value):
                    return "N/A"
                text_val = str(value).strip()
                return text_val if text_val else "N/A"

            articles_by_pmid = {
                str(row["pmid"]): row
                for _, row in df.iterrows()
                if str(row["pmid"]).strip()
            }
            new_pmids = [pmid for pmid in articles_by_pmid if pmid not in existing_pmids]

            # Generate report content from ALL fetched articles for display
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

            report_lines = [
                f"Search Query: {query}",
                f"Retrieved At (UTC): {timestamp}",
                f"Total Articles Returned: {len(articles)}",
                "",
            ]

            for index, pmid in enumerate(articles_by_pmid, start=1):
                article_lines = []
                row = articles_by_pmid[pmid]
                entities_source = row.get("entities_abstract", [])
                entities = (
                    list(entities_source)
                    if isinstance(entities_source, (list, tuple))
                    else []
                )
                if entities:
                    entities = [
                        ent.strip()
                        for ent in entities
                        if isinstance(ent, str) and ent.strip()
                    ]
                    entities = KnowledgeRepository.dedupe_preserve_order(entities)
                abstract_sections = row.get("abstract_sections", [])
                keywords = row.get("keywords", [])
                references = row.get("references", [])
                figures = row.get("figures", [])
                conflict = row.get("conflict_of_interest", "")
                article_lines.extend(
                    [
                        f"Article {index}",
                        f"Title: {safe_value(row['title'])}",
                        f"Authors: {safe_value(row['authors'])}",
                        f"DOI: {safe_value(row['doi'])}",
                        f"PMID: {safe_value(row['pmid'])}",
                        "Abstract: "
                        + KnowledgeRepository.strip_leading_label(safe_value(row["abstract"]), "Abstract"),
                        f"Entities: {', '.join(entities) if entities else 'N/A'}",
                        "",
                    ]
                )

                sections_for_report: List[Dict[str, str]] = []
                if isinstance(abstract_sections, list):
                    seen_sections: Set[Tuple[str, str]] = set()
                    for section in abstract_sections:
                        label = (section.get("label") or "Abstract").strip() or "Abstract"
                        raw_text = (section.get("text") or "").strip()
                        if not raw_text:
                            continue
                        marker = (label.lower(), raw_text)
                        if marker in seen_sections:
                            continue
                        seen_sections.add(marker)
                        sections_for_report.append({"label": label, "text": raw_text})
                    if (
                        len(sections_for_report) == 1
                        and sections_for_report[0]["label"].lower() == "abstract"
                    ):
                        sections_for_report = []

                if sections_for_report:
                    article_lines.append("Abstract Sections:")
                    for section in sections_for_report:
                        label = section.get("label") or "Abstract"
                        section_text = safe_value(section.get("text"))
                        article_lines.append(f"- {label}: {section_text}")
                    article_lines.append("")

                if isinstance(keywords, (list, tuple)) and keywords:
                    keyword_list = [
                        kw.strip()
                        for kw in keywords
                        if isinstance(kw, str) and kw.strip()
                    ]
                    keyword_list = KnowledgeRepository.dedupe_preserve_order(keyword_list)
                    if keyword_list:
                        article_lines.append(f"Keywords: {', '.join(keyword_list)}")
                        article_lines.append("")

                if conflict:
                    article_lines.append(f"Conflict of Interest: {safe_value(conflict)}")
                    article_lines.append("")

                if isinstance(figures, list) and figures:
                    article_lines.append("Figures:")
                    for fig in figures:
                        label = fig.get("label") or "Figure"
                        caption = safe_value(fig.get("caption"))
                        url = safe_value(fig.get("url"))
                        article_lines.append(f"- {label}: {caption}")
                        article_lines.append(f"  URL: {url}")
                    article_lines.append("")

                if isinstance(references, list) and references:
                    article_lines.append("References:")
                    for ref in references:
                        citation = safe_value(ref.get("citation"))
                        ref_ids = ref.get("ids")
                        id_str = (
                             ", ".join(ref_ids)
                            if isinstance(ref_ids, (list, tuple)) and ref_ids
                            else "N/A"
                        )
                        article_lines.append(f"- {citation} (IDs: {id_str})")
                    article_lines.append("")
                
                report_lines.extend(article_lines)

            report_content = "\n".join(report_lines)

            response_sections: List[str] = []
            
            # 1. Include Existing Knowledge (RAG)
            if existing_text:
                response_sections.append(f"**Existing Knowledge Base Records**:\n{existing_text}")

            # 2. Archive New Articles (one file per PMID)
            if new_pmids:
                await emitter.progress_update(f"🆕 Archiving {len(new_pmids)} new article(s)...")
                
                stored_articles = []
                for idx, pmid in enumerate(new_pmids, 1):
                    row = articles_by_pmid[pmid]
                    
                    # Generate individual article content
                    article_content_parts = [
                        f"PMID: {pmid}",
                        f"Title: {safe_value(row['title'])}",
                        f"Authors: {safe_value(row['authors'])}",
                        f"DOI: {safe_value(row['doi'])}",
                        f"Journal: {safe_value(row.get('journal', 'N/A'))}",
                        f"Search Query: {query}",
                        f"Retrieved: {timestamp}",
                        "",
                    ]
                    
                    # Add abstract - prefer structured sections for readability, fall back to combined
                    abstract_sections = row.get("abstract_sections", [])
                    sections_for_article = []
                    if isinstance(abstract_sections, list):
                        seen_sections = set()
                        for section in abstract_sections:
                            label = (section.get("label") or "Abstract").strip() or "Abstract"
                            raw_text = (section.get("text") or "").strip()
                            if not raw_text:
                                continue
                            marker = (label.lower(), raw_text)
                            if marker in seen_sections:
                                continue
                            seen_sections.add(marker)
                            sections_for_article.append({"label": label, "text": raw_text})
                        # Only skip sections if it's a single unlabeled "Abstract"
                        if len(sections_for_article) == 1 and sections_for_article[0]["label"].lower() == "abstract":
                            sections_for_article = []
                    
                    # Show structured sections if available (more readable), otherwise combined abstract
                    if sections_for_article:
                        article_content_parts.append("Abstract Sections:")
                        for section in sections_for_article:
                            label = section.get("label") or "Abstract"
                            section_text = safe_value(section.get("text"))
                            article_content_parts.append(f"- {label}: {section_text}")
                        article_content_parts.append("")
                    else:
                        # No structured sections, fall back to combined abstract
                        abstract_text = safe_value(row["abstract"])
                        if abstract_text and abstract_text != "N/A":
                            article_content_parts.append("Abstract: " + KnowledgeRepository.strip_leading_label(abstract_text, "Abstract"))
                            article_content_parts.append("")
                        else:
                            # No abstract available at all
                            article_content_parts.append("Abstract: N/A")
                            article_content_parts.append("")
                    
                    # Add entities
                    entities_source = row.get("entities_abstract", [])
                    entities = list(entities_source) if isinstance(entities_source, (list, tuple)) else []
                    if entities:
                        entities = [e.strip() for e in entities if isinstance(e, str) and e.strip()]
                        entities = KnowledgeRepository.dedupe_preserve_order(entities)
                        article_content_parts.append(f"Entities: {', '.join(entities)}")
                        article_content_parts.append("")
                    
                    # Add keywords
                    keywords = row.get("keywords", [])
                    if isinstance(keywords, (list, tuple)) and keywords:
                        kw_list = [kw.strip() for kw in keywords if isinstance(kw, str) and kw.strip()]
                        kw_list = KnowledgeRepository.dedupe_preserve_order(kw_list)
                        if kw_list:
                            article_content_parts.append(f"Keywords: {', '.join(kw_list)}")
                            article_content_parts.append("")
                    
                    # Add references
                    references = row.get("references", [])
                    if isinstance(references, list) and references:
                        article_content_parts.append("References:")
                        for ref in references[:10]:  # Limit to first 10 refs
                            citation = safe_value(ref.get("citation"))
                            article_content_parts.append(f"- {citation}")
                        article_content_parts.append("")
                    
                    article_content = "\n".join(article_content_parts)
                    
                    # Prepare metadata with PMID for deduplication
                    article_metadata = {
                        "pmid": pmid,
                        "query": query,
                        "title": safe_value(row["title"])[:500],  # Limit for metadata
                        "journal": safe_value(row.get("journal", "")),
                        "type": "pubmed_article",
                        "source": "pubmed",
                    }
                    
                    # Generate unique filename per PMID
                    title_slug = re.sub(r"[^A-Za-z0-9]+", "_", safe_value(row["title"]))[:50]
                    article_filename = f"PMID_{pmid}_{title_slug}.txt"
                    
                    print(f"[PUBMED] Uploading article {idx}/{len(new_pmids)}: PMID {pmid}")
                    try:
                        file_record = await KnowledgeRepository.upload_report_file(
                            request=request,
                            user=user,
                            filename=article_filename,
                            content=article_content,
                            metadata=article_metadata,
                        )
                        print(f"[PUBMED] File uploaded: ID={file_record['id']}")
                        
                        await KnowledgeRepository.attach_file_to_knowledge(
                            request=request,
                            user=user,
                            kb_id=kb.id,
                            file_id=file_record["id"],
                            content=article_content,
                        )
                        print(f"[PUBMED] Article PMID {pmid} attached and processed")
                        
                        stored_articles.append({
                            "pmid": pmid,
                            "title": safe_value(row["title"]),
                            "authors": safe_value(row["authors"]),
                            "doi": safe_value(row["doi"]),
                        })
                        
                        if idx % 5 == 0 or idx == len(new_pmids):
                            await emitter.progress_update(f"📥 Archived {idx}/{len(new_pmids)} articles...")
                    
                    except Exception as upload_error:
                        print(f"[PUBMED] Error uploading PMID {pmid}: {upload_error}")
                        await emitter.progress_update(f"⚠️ Warning: Failed to archive PMID {pmid}")
                        continue
                
                # Build summary of stored articles
                new_summaries = []
                for article in stored_articles:
                    new_summaries.append(
                        f"Title: {article['title']}\n"
                        f"Authors: {article['authors']}\n"
                        f"PMID: {article['pmid']}\n"
                        f"DOI: {article['doi']}\n"
                    )
                
                if stored_articles:
                    response_sections.append(
                        f"**New Articles Archived ({timestamp})**:\n"
                        f"Successfully stored {len(stored_articles)} article(s):\n\n"
                        + "\n".join(new_summaries)
                    )
                else:
                    response_sections.append("**Archive Error**: Failed to store new articles.")
            else:
                await emitter.progress_update("ℹ️ No new articles compared to stored history")
                response_sections.append("**Update Notice**: No new PubMed articles were found. Existing snapshot remains current.")

            # 3. Retrieve Updated RAG (to confirm storage/retrieval)
            await emitter.progress_update("🔄 Retrieving latest knowledge snapshot...")
            print(f"[PUBMED] Starting RAG retrieval for query: {query}")
            try:
                updated_rag = await KnowledgeRepository.query_knowledge_base(
                    request=request,
                    user=user,
                    kb_id=kb.id,
                    query=query,
                    limit=self.valves.max_results,
                    valves=self.valves,
                )
                chunk_count = len(updated_rag.get('documents', [[]])[0])
                print(f"[PUBMED] RAG retrieval successful: {chunk_count} document chunks")
            except Exception as rag_error:
                print(f"[PUBMED] RAG retrieval error (non-fatal): {type(rag_error).__name__}: {rag_error}")
                updated_rag = {"documents": [[]]}
                # Continue anyway - the files are stored even if RAG query fails
            updated_docs = updated_rag.get("documents", [])
            if updated_docs and updated_docs[0]:
                combined = "\n\n".join(updated_docs[0])
                # Only show if different from what we already showed in "Existing Records"
                if not existing_text or combined != existing_text:
                    response_sections.append(
                        f"**Updated Knowledge Snapshot**:\n{combined}"
                    )

            await emitter.success_update("✅ Research complete!")

            # 4. Construct Final Output
            # We include the full report content at the end to ensure the model has the actual data 
            # (Abstracts, etc.) even if the RAG chunks were fragmented or just headers.
            result_text = (
                f"🔬 **PubMed Deep Research Results** — {query}\n\n"
                + "\n\n".join(response_sections) + "\n\n"
                + "--- **Full Report of Current Search** ---\n"
                + "(This section contains the full text of the articles found in this session)\n\n"
                + report_content
            )
            
            await eventer(
                {
                    "type": "result",
                    "data": {
                        "description": pubmed_debug + result_text,
                        "done": True,
                        "hidden": False,
                    },
                }
            )
            return pubmed_debug + result_text

        except (requests_exceptions.RequestException, ET.ParseError, ValueError) as exc:
            # Known operational errors: network, XML parsing, validation
            await emitter.error_update(f"❌ Error: {exc}")
            error_msg = f"❌ **PubMed Tool Error**: {exc}"
            await eventer(
                {
                    "type": "result",
                    "data": {"description": error_msg, "done": True, "hidden": False},
                }
            )
            return pubmed_debug + error_msg
        except Exception as exc:  # Fallback unexpected error # noqa: BLE001
            await emitter.error_update(f"❌ Unexpected Error: {exc}")
            error_msg = f"❌ **PubMed Tool Unexpected Error**: {exc}"
            await eventer(
                {
                    "type": "result",
                    "data": {"description": error_msg, "done": True, "hidden": False},
                }
            )
            return pubmed_debug + error_msg


class KnowledgeRepository:
    """Helper for resolving OpenWebUI knowledge bases without exposing extra tool methods."""

    @staticmethod
    async def load_by_user(
        user_id: Any, permission: str = "write"
    ) -> List[KnowledgeUserModel]:
        knowledge = await run_in_threadpool(
            Knowledges.get_knowledge_bases_by_user_id, user_id, permission
        )
        return knowledge or []

    @staticmethod
    async def find_by_name(
        user_id: Any, identifier: str, permission: str = "write"
    ) -> Optional[KnowledgeUserModel]:
        knowledge_bases = await KnowledgeRepository.load_by_user(user_id, permission)
        if not knowledge_bases:
            return None

        normalized = identifier.strip().lower()
        id_lookup = {kb.id: kb for kb in knowledge_bases}
        if identifier in id_lookup:
            return id_lookup[identifier]

        by_name = {
            (kb.name or "").strip().lower(): kb for kb in knowledge_bases if kb.name
        }
        return by_name.get(normalized)

    @staticmethod
    async def create_knowledge_base(
        user_id: Any, name: str, description: str = ""
    ) -> KnowledgeUserModel:
        """Create a new knowledge base for the user."""
        from open_webui.models.knowledge import KnowledgeForm
        
        knowledge_form = KnowledgeForm(
            name=name,
            description=description,
            data={},
        )
        
        kb = await run_in_threadpool(
            Knowledges.insert_new_knowledge,
            user_id,
            knowledge_form,
        )
        
        if not kb:
            raise ValueError(f"Failed to create knowledge base '{name}'")
        
        return kb

    # -------- Generic parsing helpers (moved from tool surface) -------- #
    @staticmethod
    def extract_text(elem: Optional[Any]) -> str:
        if elem is None:
            return ""
        try:
            return "".join(elem.itertext()).strip()
        except (AttributeError, TypeError):
            return (getattr(elem, "text", "") or "").strip()

    @staticmethod
    def strip_namespace(tag: Any) -> str:
        if isinstance(tag, str) and "}" in tag:
            return tag.split("}", 1)[-1]
        return tag if isinstance(tag, str) else str(tag)

    @staticmethod
    def dedupe_preserve_order(values: List[Any]) -> List[Any]:
        seen: Set[str] = set()
        unique: List[Any] = []
        for value in values:
            marker = repr(value)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(value)
        return unique

    @staticmethod
    def strip_leading_label(value: str, label: str) -> str:
        if not value or not label:
            return value
        normalized = value.strip()
        prefix = f"{label.strip()}:"
        if normalized.lower().startswith(prefix.lower()):
            return normalized[len(prefix):].lstrip()
        return value

    # -------- Moved parsing & figure helpers -------- #
    @staticmethod
    def parse_medline_article(xml_payload: bytes) -> Dict[str, Any]:
        details: Dict[str, Any] = {
            "abstract": "",
            "abstract_sections": [],
            "keywords": [],
            "conflict_of_interest": "",
            "references": [],
            "pmcid": None,
            "article_ids": [],
            "journal": "",
            "publication_types": [],
        }
        try:
            root = ET.fromstring(xml_payload)
        except ET.ParseError:
            return details

        article = root.find(".//PubmedArticle")
        if article is None:
            return details

        abstract_sections: List[Dict[str, str]] = []
        # Handle structured abstracts with separate AbstractText elements
        for abstract_elem in article.findall(".//Abstract/AbstractText"):
            label = abstract_elem.attrib.get("Label") or abstract_elem.attrib.get("NlmCategory")
            section_text = KnowledgeRepository.extract_text(abstract_elem)
            abstract_sections.append({"label": label or "Abstract", "text": section_text})
        
        # If we got sections, build combined abstract
        if abstract_sections:
            combined_sections = []
            for section in abstract_sections:
                sec_label = section.get("label") or "Abstract"
                sec_text = section.get("text") or ""
                combined_sections.append(f"{sec_label}: {sec_text}" if sec_text else sec_label)
            details["abstract"] = "\n".join([part.strip() for part in combined_sections if part]).strip()
        else:
            # No structured sections - check for single AbstractText
            abstract_elem = article.find(".//AbstractText")
            if abstract_elem is not None:
                # Try to extract inline sections (e.g., <b>Background:</b> text <b>Methods:</b> text)
                full_text = KnowledgeRepository.extract_text(abstract_elem)
                details["abstract"] = full_text
                
                # Parse inline sections if present (common pattern: <b>Label:</b> text)
                if "<b>" in ET.tostring(abstract_elem, encoding='unicode', method='xml'):
                    inline_sections = []
                    current_label = None
                    current_text = []
                    
                    for child in abstract_elem:
                        if child.tag == 'b':
                            # Save previous section if exists
                            if current_label and current_text:
                                inline_sections.append({
                                    "label": current_label.rstrip(':'),
                                    "text": ' '.join(current_text).strip()
                                })
                                current_text = []
                            # Start new section
                            current_label = (child.text or '').strip()
                            # Get text after the <b> tag
                            if child.tail:
                                current_text.append(child.tail.strip())
                        elif current_label:
                            # Continue current section with regular text
                            if child.text:
                                current_text.append(child.text.strip())
                            if child.tail:
                                current_text.append(child.tail.strip())
                    
                    # Save last section
                    if current_label and current_text:
                        inline_sections.append({
                            "label": current_label.rstrip(':'),
                            "text": ' '.join(current_text).strip()
                        })
                    
                    # Use inline sections if we found any
                    if inline_sections:
                        abstract_sections = inline_sections
        
        details["abstract_sections"] = abstract_sections

        keyword_values = [kw.text.strip() for kw in article.findall(".//Keyword") if kw.text]
        details["keywords"] = KnowledgeRepository.dedupe_preserve_order(keyword_values)

        coi_elem = article.find(".//CoiStatement")
        details["conflict_of_interest"] = KnowledgeRepository.extract_text(coi_elem)

        references: List[Dict[str, Any]] = []
        for ref in article.findall(".//Reference"):
            citation = KnowledgeRepository.extract_text(ref.find("Citation"))
            ids = [id_elem.text.strip() for id_elem in ref.findall(".//ArticleId") if id_elem.text]
            references.append({"citation": citation, "ids": ids})
        details["references"] = references

        article_ids: List[Dict[str, str]] = []
        pmcid_val = None
        for id_elem in article.findall(".//ArticleId"):
            id_type = id_elem.attrib.get("IdType", "").strip()
            value = (id_elem.text or "").strip()
            if not value:
                continue
            article_ids.append({"type": id_type or "unknown", "value": value})
            if id_type.lower() == "pmc":
                pmcid_val = value if value.upper().startswith("PMC") else f"PMC{value}"
        details["article_ids"] = article_ids
        details["pmcid"] = pmcid_val

        journal = article.find(".//Journal/Title")
        details["journal"] = KnowledgeRepository.extract_text(journal)

        pub_types = [KnowledgeRepository.extract_text(pt) for pt in article.findall(".//PublicationType") if KnowledgeRepository.extract_text(pt)]
        details["publication_types"] = KnowledgeRepository.dedupe_preserve_order(pub_types)
        return details

    @staticmethod
    def fetch_pmc_figures(pmcid: Optional[str], valves: Any) -> List[Dict[str, str]]:
        if not pmcid:
            return []
        base_url = getattr(valves, "pubmed_base_url", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/")
        api_key = getattr(valves, "pubmed_api_key", None)
        params = {"db": "pmc", "id": pmcid, "retmode": "xml", "api_key": api_key or None}
        response = requests.get(f"{base_url}/efetch.fcgi", params=params, timeout=10)
        if response.status_code != 200:
            return []
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            return []
        figures: List[Dict[str, str]] = []
        for fig in root.iter():
            if KnowledgeRepository.strip_namespace(fig.tag) != "fig":
                continue
            label_elem = next((child for child in fig if KnowledgeRepository.strip_namespace(child.tag) == "label"), None)
            caption_elem = next((child for child in fig if KnowledgeRepository.strip_namespace(child.tag) == "caption"), None)
            graphic_elem = None
            for descendant in fig.iter():
                if KnowledgeRepository.strip_namespace(descendant.tag) == "graphic":
                    graphic_elem = descendant
                    break
            figure_url = ""
            if graphic_elem is not None:
                href_value = ""
                for attr_key, attr_val in graphic_elem.attrib.items():
                    if attr_key.lower().endswith("href") and attr_val:
                        href_value = attr_val
                        break
                if href_value:
                    if href_value.startswith("http"):
                        figure_url = href_value
                    elif href_value.startswith("/"):
                        figure_url = f"https://www.ncbi.nlm.nih.gov{href_value}"
                    else:
                        figure_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/{href_value}"
            figures.append({
                "label": KnowledgeRepository.extract_text(label_elem) or fig.attrib.get("id", ""),
                "caption": KnowledgeRepository.extract_text(caption_elem),
                "url": figure_url,
            })
        return [fig for fig in figures if fig.get("url")]

    # -------- Context resolution helpers -------- #
    @staticmethod
    async def resolve_user(__user__: Optional[dict]) -> Any:
        if not __user__ or not __user__.get("id"):
            raise ValueError("User context with an 'id' is required")
        user = await run_in_threadpool(Users.get_user_by_id, str(__user__["id"]))
        if not user:
            raise ValueError("Unable to resolve OpenWebUI user")
        return user

    @staticmethod
    def require_request(__request__: Optional[Any]) -> Any:
        if __request__ is None or not isinstance(__request__, Request):
            raise ValueError("Request context is required inside OpenWebUI")
        return __request__

    # -------- Query variation helpers -------- #
    @staticmethod
    def get_pubmed_spell_suggestion(query: str, valves: Any) -> Optional[str]:
        """Get spelling suggestions from PubMed ESpell."""
        try:
            base_url = getattr(valves, "pubmed_base_url", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils")
            api_key = getattr(valves, "pubmed_api_key", None)
            params = {
                "db": "pubmed",
                "term": query,
                "api_key": api_key or None,
            }
            response = requests.get(f"{base_url}/espell.fcgi", params=params, timeout=10)
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                corrected = root.find("CorrectedQuery")
                if corrected is not None and corrected.text:
                    suggested = corrected.text.strip()
                    # Only return if actually different
                    if suggested.lower() != query.lower():
                        return suggested
        except Exception:
            pass
        return None

    @staticmethod
    def generate_query_variations(query: str) -> List[str]:
        """Generate variations of the search query using spaCy and simple heuristics."""
        variations = []
        
        # Parse with spaCy to extract entities and key terms
        doc = nlp(query.lower())
        
        # Strategy 1: Remove year/date constraints if present
        date_pattern = r'\b(19|20)\d{2}\b|\b(\d{4}):(\d{4})\b'
        if re.search(date_pattern, query):
            variation = re.sub(date_pattern, '', query).strip()
            variation = re.sub(r'\s+', ' ', variation)  # Clean extra spaces
            if variation and variation.lower() != query.lower():
                variations.append(variation)
        
        # Strategy 2: Broaden by removing field tags like [journal], [author], etc.
        field_pattern = r'\[\w+\]'
        if re.search(field_pattern, query):
            variation = re.sub(field_pattern, '', query).strip()
            variation = re.sub(r'\s+', ' ', variation)
            if variation and variation.lower() != query.lower():
                variations.append(variation)
        
        # Strategy 3: Remove AND/OR Boolean operators to broaden
        if ' AND ' in query.upper() or ' OR ' in query.upper():
            # Replace AND with OR to broaden
            variation = re.sub(r'\s+AND\s+', ' OR ', query, flags=re.IGNORECASE)
            if variation.lower() != query.lower():
                variations.append(variation)
            
            # Remove AND entirely
            variation = re.sub(r'\s+AND\s+', ' ', query, flags=re.IGNORECASE)
            variation = re.sub(r'\s+', ' ', variation).strip()
            if variation and variation.lower() != query.lower():
                variations.append(variation)
        
        # Strategy 4: Use just the main entities/noun phrases
        entities = [ent.text for ent in doc.ents if ent.label_ in ['DISEASE', 'CHEMICAL', 'ORG', 'GPE']]
        noun_chunks = [chunk.text for chunk in doc.noun_chunks]
        key_terms = entities + noun_chunks
        
        if key_terms:
            # Try just the entities
            variation = ' '.join(entities) if entities else None
            if variation and variation.lower() != query.lower():
                variations.append(variation)
            
            # Try noun phrases
            if len(noun_chunks) > 0 and len(noun_chunks) <= 3:
                variation = ' '.join(noun_chunks)
                if variation.lower() != query.lower():
                    variations.append(variation)
        
        # Deduplicate and limit
        seen = set([query.lower()])
        unique_variations = []
        for var in variations:
            var_lower = var.lower().strip()
            if var_lower and var_lower not in seen and len(var_lower) > 3:
                seen.add(var_lower)
                unique_variations.append(var)
        
        return unique_variations[:4]  # Limit to 4 variations

    # -------- Knowledge base interaction wrappers -------- #
    @staticmethod
    async def get_existing_pmids_from_kb(kb_id: str) -> Set[str]:
        """Get all PMIDs already stored in the knowledge base by checking file metadata."""
        pmids = set()
        try:
            knowledge = await run_in_threadpool(Knowledges.get_knowledge_by_id, kb_id)
            if knowledge:
                data = getattr(knowledge, "data", None) or {}
                file_ids = data.get("file_ids", [])
                
                if file_ids:
                    from open_webui.models.files import Files
                    for file_id in file_ids:
                        file_record = await run_in_threadpool(Files.get_file_by_id, file_id)
                        if file_record:
                            file_meta = getattr(file_record, "meta", None) or {}
                            pmid = file_meta.get("pmid")
                            if pmid:
                                pmids.add(str(pmid))
        except Exception:
            # If we can't get PMIDs from metadata, fall back to text search
            pass
        return pmids

    @staticmethod
    async def query_knowledge_base(
        request: Any,
        user: Any,
        kb_id: str,
        query: str,
        limit: int,
        valves: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # Build form kwargs using valves if provided, else safe defaults
        max_results = getattr(valves, "max_results", limit) if valves else limit
        form_kwargs: Dict[str, Any] = {
            "collection_names": [kb_id],
            "query": query,
            "k": max(limit, max_results),
            "hybrid": getattr(valves, "enable_hybrid_search", False) if valves else False,
        }
        # Add BM25 weight if hybrid search is enabled
        if getattr(valves, "enable_hybrid_search", False) if valves else False:
            bm25_weight = getattr(valves, "hybrid_bm25_weight", 0.5) if valves else 0.5
            form_kwargs["hybrid_bm25_weight"] = bm25_weight
            # Add enriched texts option for hybrid search
            enriched = getattr(valves, "enable_enriched_hybrid_search", False) if valves else False
            form_kwargs["enable_enriched_texts"] = enriched
        
        reranker = getattr(valves, "reranker_results", 0) if valves else 0
        if reranker > 0:
            form_kwargs["k_reranker"] = reranker
        relevance = getattr(valves, "relevance_threshold", 0.0) if valves else 0.0
        if relevance > 0:
            form_kwargs["r"] = relevance
        form = QueryCollectionsForm(**form_kwargs)
        return await query_collection_handler(request=request, form_data=form, user=user)

    @staticmethod
    async def upload_report_file(
        request: Any,
        user: Any,
        filename: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> Dict[str, Any]:
        # Ensure metadata is safe for ChromaDB (no None values)
        safe_metadata = metadata.copy() if metadata else {}
        safe_metadata.setdefault("source", "pubmed_tool")
        safe_metadata.setdefault("type", "text")
        
        final_metadata = {}
        for k, v in safe_metadata.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                final_metadata[k] = v
            else:
                final_metadata[k] = str(v)

        upload = UploadFile(
            filename=filename,
            file=SpooledTemporaryFile(max_size=1024 * 1024),
            headers={"content-type": "text/plain"},
        )
        upload.file.write(content.encode("utf-8"))
        upload.file.seek(0)
        try:
            result = await run_in_threadpool(
                upload_file_handler,
                request,
                upload,
                final_metadata,
                False, # process
                False, # process_in_background
                user,
                None,
            )
        finally:
            await upload.close()
        # Handle Pydantic model (OpenWebUI 0.6.x) or dict
        file_id = getattr(result, "id", None)
        if file_id is None and isinstance(result, dict):
            file_id = result.get("id")

        if not file_id:
            raise ValueError("Failed to upload report content into OpenWebUI files")

        # Return dict for compatibility
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if hasattr(result, "dict"):
            return result.dict()
        return result

    @staticmethod
    async def attach_file_to_knowledge(
        request: Any,
        user: Any,
        kb_id: str,
        file_id: str,
        content: Optional[str] = None,
    ) -> None:
        # 1. Associate file with knowledge base
        try:
            await run_in_threadpool(
                Knowledges.add_file_to_knowledge_by_id,
                kb_id,
                file_id,
                user.id,
            )
        except AttributeError:
            # Fallback for older versions or if method missing
            def _update_metadata() -> bool:
                knowledge = Knowledges.get_knowledge_by_id(id=kb_id)
                if not knowledge:
                    return False
                data = getattr(knowledge, "data", None) or {}
                file_ids = data.get("file_ids", [])
                if file_id not in file_ids:
                    file_ids.append(file_id)
                    data["file_ids"] = file_ids
                    Knowledges.update_knowledge_data_by_id(id=kb_id, data=data)
                return True

            updated = await run_in_threadpool(_update_metadata)
            if not updated:
                raise ValueError("Failed to update knowledge metadata with new file")

        # 2. Process file for this knowledge base
        await run_in_threadpool(
            process_file,
            request,
            ProcessFileForm(file_id=file_id, collection_name=kb_id, content=content),
            user,
        )
