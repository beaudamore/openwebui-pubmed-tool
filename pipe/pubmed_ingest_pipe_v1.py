"""
title: PubMed Ingest Pipe
author: Beau D'Amore www.damore.ai
version: 1.0.0
description: Pulls PubMed articles for one or more configured search queries, deduplicates against a knowledge base by PMID, archives new articles (with NLP entities/keywords and optional PMC figures), and emails a confirmation via the Gmail API (OAuth2).
requirements: pandas, spacy, nltk, requests

Ports the search/NLP/archive engine from the PubMed Deep Research Tool
(openwebui-pubmed-tool/tool/pubmed_internal_v3.py) into a native OWUI "Pipe"
(Admin -> Functions -> type: pipe), following the pull -> dedupe -> ingest ->
email-confirmation shape of openwebui-feed-ingest-pipe.

This registers as its own selectable model. Selecting it and sending any
message runs the sync -- OpenWebUI calls pipe() directly, so no LLM inference
happens. The returned summary IS the entire chat response.

Dedup fix vs. the original tool: `upload_file_handler` stores any custom
metadata dict nested under `file.meta["data"]`, never at the top level of
`meta` (see open_webui/routers/files.py). The original tool's
`get_existing_pmids_from_kb` read `file_meta.get("pmid")` at the top level,
which can never match -- that lookup was silently dead code, saved only by
its filename-regex fallback ("PMID_<pmid>_<slug>.txt"). This pipe reads the
correct nested location (`file_meta["data"]["pmid"]`) as the primary check,
keeping the filename-regex fallback for resilience against older/foreign
files. The original's supplemental RAG-text-scan layer (regex over the
current query's retrieved documents) is dropped -- it was a query-scoped
heuristic, not a full-corpus source of truth, and is redundant now that the
metadata read is correct.
"""

import base64
import io
import logging
import os
import re
import tarfile
import xml.etree.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from tempfile import SpooledTemporaryFile
from typing import Any, Dict, List, Optional, Set

import nltk
import pandas as pd
import requests
import spacy
from requests import exceptions as requests_exceptions
from fastapi import Request, UploadFile
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from pydantic import BaseModel, Field
from sqlalchemy import text

from open_webui.internal.db import get_async_db
from open_webui.models.knowledge import Knowledges, KnowledgeUserModel
from open_webui.models.users import Users
from open_webui.routers.files import upload_file_handler
from open_webui.routers.retrieval import ProcessFileForm, process_file

logger = logging.getLogger("pubmed_ingest_pipe")

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


class Pipe:
    class Valves(BaseModel):
        model_config = {"arbitrary_types_allowed": True}

        priority: int = 0
        enabled: bool = True

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
            description="Knowledge base name to search/store data. Created automatically if it doesn't exist.",
        )
        search_queries: List[str] = Field(
            default_factory=list,
            description="Default PubMed search queries to sync each run. Used when the chat message sent to this pipe is empty; otherwise the message text is used as a one-off query instead.",
        )
        enable_query_variation: bool = Field(
            default=True,
            description="Automatically try alternative search queries if no new results are found. Uses PubMed spell check and query broadening.",
        )
        fetch_multiplier: float = Field(
            default=2.5,
            description="Multiplier for initial PubMed fetch to account for duplicates. Set to 1.0 to disable.",
        )
        max_query_attempts: int = Field(
            default=3,
            description="Maximum number of query variations to try (1-5).",
        )
        max_results: int = Field(
            default=10,
            description="Maximum number of NEW results to retrieve per query (excludes articles already in the knowledge base).",
        )
        reranker_results: int = Field(
            default=0,
            description="Number of articles to include in the returned summary text per query (0 = use max_results). All fetched articles are archived to the knowledge base regardless.",
        )
        enable_figure_download: bool = Field(
            default=False,
            description="Download PMC figure images and store them in OpenWebUI's file store alongside article metadata.",
        )
        max_figures_per_article: int = Field(
            default=10,
            description="Maximum number of figures to download per article (0 = unlimited). Only applies when enable_figure_download is True.",
        )
        notify_email_to: str = Field(
            default="",
            description="Recipient email address for the confirmation email. Leave blank to skip sending.",
        )
        notify_email_subject_prefix: str = Field(
            default="PubMed Sync",
            description="Subject line prefix for the confirmation email.",
        )
        enable_debug_output: bool = Field(
            default=True,
            description="Include debug information in the returned summary.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # -------- Gmail API (OAuth2 refresh token), credentials from env only -------- #
    # Set these in the container's environment (Portainer stack env vars):
    #   SEND_EMAIL_BEAU_CLIENT_ID
    #   SEND_EMAIL_BEAU_CLIENT_SECRET
    #   SEND_EMAIL_BEAU_CLIENT_REFRESH_TOKEN
    @staticmethod
    def _get_gmail_access_token() -> tuple[Optional[str], Optional[str]]:
        client_id = os.getenv("SEND_EMAIL_BEAU_CLIENT_ID", "")
        client_secret = os.getenv("SEND_EMAIL_BEAU_CLIENT_SECRET", "")
        refresh_token = os.getenv("SEND_EMAIL_BEAU_CLIENT_REFRESH_TOKEN", "")
        if not client_id or not client_secret or not refresh_token:
            logger.error("gmail token refresh: missing one or more SEND_EMAIL_BEAU_* env vars")
            return None, "SEND_EMAIL_BEAU_CLIENT_ID / _CLIENT_SECRET / _CLIENT_REFRESH_TOKEN not set in environment"

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("gmail token refresh failed: %s %s", resp.status_code, resp.text)
            return None, f"Token refresh failed: {resp.status_code} {resp.text}"
        return resp.json().get("access_token"), None

    @staticmethod
    def _send_confirmation_email(subject: str, body: str, to_addr: str) -> Optional[str]:
        if not to_addr:
            return "No notify_email_to configured"

        access_token, err = Pipe._get_gmail_access_token()
        if err:
            return err

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = to_addr
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        resp = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.error("send_confirmation_email: gmail send failed: %s %s", resp.status_code, resp.text)
            return f"Gmail send failed: {resp.status_code} {resp.text}"
        return None

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __request__: Optional[Any] = None,
    ) -> str:
        """
        Entry point OpenWebUI calls when this Pipe's model is selected. Runs
        the full pull -> dedupe -> NLP process -> ingest -> email pipeline
        and returns a natural-language summary as the entire chat response.
        No underlying LLM is called.
        """
        async def _noop_event(*_args, **_kwargs):
            return None

        eventer = __event_emitter__ if __event_emitter__ is not None else _noop_event
        emitter = EventEmitter(eventer)

        if not self.valves.enabled:
            return "PubMed sync is currently disabled (see the 'enabled' valve)."

        try:
            request = KnowledgeRepository.require_request(__request__)
            user = await KnowledgeRepository.resolve_user(__user__)
        except ValueError as exc:
            msg = f"❌ {exc}"
            await emitter.error_update(msg)
            return msg

        if not self.valves.default_knowledge_base:
            msg = "❌ No 'default_knowledge_base' configured."
            await emitter.error_update(msg)
            return msg

        messages = body.get("messages") or []
        user_text = ""
        if messages:
            last = messages[-1]
            if isinstance(last, dict):
                content = last.get("content")
                if isinstance(content, str):
                    user_text = content.strip()

        queries = [user_text] if user_text else list(self.valves.search_queries)
        if not queries:
            return (
                "❌ No search query in the chat message and no 'search_queries' "
                "configured on the pipe. Either send a PubMed query as your message, "
                "or set default queries in the valves."
            )

        try:
            kb = await KnowledgeRepository.get_or_create_knowledge_base(
                user_id=user.id,
                name=self.valves.default_knowledge_base,
                description="Auto-created by PubMed Ingest Pipe",
            )
        except Exception as exc:
            msg = f"❌ Failed to resolve knowledge base: {exc}"
            await emitter.error_update(msg)
            return msg

        sections: List[str] = []
        email_lines: List[str] = []
        total_new = 0

        for query in queries:
            try:
                section_text, new_count, ingested_lines = await self._research_query(
                    query=query,
                    kb=kb,
                    user=user,
                    request=request,
                    emitter=emitter,
                )
            except (requests_exceptions.RequestException, ET.ParseError, ValueError) as exc:
                section_text = f"❌ **PubMed Ingest Error** for '{query}': {exc}"
                new_count = 0
                ingested_lines = []
                await emitter.error_update(section_text)
            except Exception as exc:  # noqa: BLE001
                section_text = f"❌ **PubMed Ingest Unexpected Error** for '{query}': {exc}"
                new_count = 0
                ingested_lines = []
                await emitter.error_update(section_text)

            sections.append(section_text)
            total_new += new_count
            email_lines.extend(ingested_lines)

        await emitter.success_update("✅ Sync complete!")

        result_text = "\n\n---\n\n".join(sections)

        err = self._send_confirmation_email(
            subject=f"{self.valves.notify_email_subject_prefix}: {total_new} new article(s)",
            body="\n".join(email_lines) if email_lines else "No new articles.",
            to_addr=self.valves.notify_email_to,
        )
        if err:
            result_text += f"\n\n⚠️ The confirmation email could not be sent: {err}"
        elif self.valves.notify_email_to:
            result_text += f"\n\nA confirmation email was sent to {self.valves.notify_email_to}."

        return result_text

    async def _research_query(
        self,
        query: str,
        kb: Any,
        user: Any,
        request: Any,
        emitter: "EventEmitter",
    ) -> tuple[str, int, List[str]]:
        """Run one query's pull -> dedupe -> NLP -> archive pass. Returns
        (summary_section_text, new_article_count, ["PMID x: title", ...])."""

        effective_max_results = self.valves.max_results

        await emitter.progress_update(f"🔬 Researching '{query}'...")

        # Corrected, metadata-based dedup (matches upload_file_handler's real
        # storage location) with a filename-regex fallback for resilience.
        existing_pmids = await KnowledgeRepository.get_existing_pmids_from_kb(kb.id)
        if existing_pmids:
            await emitter.progress_update(f"📚 Found {len(existing_pmids)} existing article(s) in KB")

        fetch_limit = effective_max_results
        if existing_pmids and self.valves.fetch_multiplier > 1.0:
            fetch_limit = int(effective_max_results * self.valves.fetch_multiplier)

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
                            for author in docsum.findall('.//Item[@Name="AuthorList"]/Item')
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
                    medline_details = KnowledgeRepository.parse_medline_article(fetch_response.content)
                abstract = medline_details.get("abstract", "")
                figures = KnowledgeRepository.fetch_pmc_figures(medline_details.get("pmcid"), self.valves)

                results.append(
                    {
                        "pmid": pmid,
                        "title": title,
                        "authors": authors,
                        "abstract": abstract,
                        "doi": doi,
                        "abstract_sections": medline_details.get("abstract_sections", []),
                        "keywords": medline_details.get("keywords", []),
                        "conflict_of_interest": medline_details.get("conflict_of_interest", ""),
                        "references": medline_details.get("references", []),
                        "figures": figures,
                        "pmcid": medline_details.get("pmcid"),
                        "article_ids": medline_details.get("article_ids", []),
                        "journal": medline_details.get("journal", ""),
                        "publication_types": medline_details.get("publication_types", []),
                    }
                )

            return results

        articles: List[Dict[str, Any]] = []
        max_attempts = (
            min(max(1, self.valves.max_query_attempts), 5)
            if self.valves.enable_query_variation
            else 1
        )

        while query_attempt <= max_attempts:
            articles = pubmed_search(current_query, fetch_limit)

            if existing_pmids:
                articles = [
                    art for art in articles if str(art.get("pmid", "")).strip() not in existing_pmids
                ]
                if len(articles) > effective_max_results:
                    articles = articles[:effective_max_results]

            if articles:
                break

            if query_attempt < max_attempts and self.valves.enable_query_variation:
                if query_attempt == 1:
                    spell_suggestion = KnowledgeRepository.get_pubmed_spell_suggestion(current_query, self.valves)
                    if spell_suggestion and spell_suggestion.lower() not in [q.lower() for q in queries_tried]:
                        current_query = spell_suggestion
                        queries_tried.append(current_query)
                        query_attempt += 1
                        continue

                variations = KnowledgeRepository.generate_query_variations(query)
                untried_variations = [
                    v for v in variations if v.lower() not in [q.lower() for q in queries_tried]
                ]
                if untried_variations:
                    current_query = untried_variations[0]
                    queries_tried.append(current_query)
                    query_attempt += 1
                else:
                    break
            else:
                break

        if not articles:
            no_results_msg = "No NEW articles found" if existing_pmids else "No articles found"
            return (
                f"❌ **PubMed Ingest**: {no_results_msg} for query '{query}'.",
                0,
                [],
            )

        await emitter.progress_update(f"📊 Processing {len(articles)} article(s) with NLP for '{query}'...")

        def process_data(articles_input: List[Dict[str, str]]) -> pd.DataFrame:
            df = pd.DataFrame(articles_input)

            def clean_text(txt):
                if not txt:
                    return ""
                txt = re.sub(r"\s+", " ", txt)
                txt = re.sub(r"[^\w\s]", "", txt)
                return txt.lower()

            df["clean_title"] = df["title"].apply(clean_text)
            df["clean_abstract"] = df["abstract"].apply(clean_text)

            stop_words = set(stopwords.words("english"))

            def tokenize_and_filter(txt):
                tokens = word_tokenize(txt)
                return [word for word in tokens if word not in stop_words]

            df["tokens_title"] = df["clean_title"].apply(tokenize_and_filter)
            df["tokens_abstract"] = df["clean_abstract"].apply(tokenize_and_filter)

            def spacy_process(txt):
                doc = nlp(txt)
                lemmas = [token.lemma_ for token in doc if not token.is_stop and token.is_alpha]
                entities = [ent.text for ent in doc.ents]
                entities = KnowledgeRepository.dedupe_preserve_order(entities)
                return " ".join(lemmas), entities

            df["lemmas_abstract"], df["entities_abstract"] = zip(*df["clean_abstract"].apply(spacy_process))
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
            str(row["pmid"]): row for _, row in df.iterrows() if str(row["pmid"]).strip()
        }
        new_pmids = [pmid for pmid in articles_by_pmid if pmid not in existing_pmids]

        output_limit = self.valves.reranker_results if self.valves.reranker_results > 0 else effective_max_results
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        stored_articles: List[Dict[str, Any]] = []
        email_lines: List[str] = []

        await emitter.progress_update(f"🆕 Archiving {len(new_pmids)} new article(s) for '{query}'...")

        for idx, pmid in enumerate(new_pmids, 1):
            row = articles_by_pmid[pmid]

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
                if len(sections_for_article) == 1 and sections_for_article[0]["label"].lower() == "abstract":
                    sections_for_article = []

            if sections_for_article:
                article_content_parts.append("Abstract Sections:")
                for section in sections_for_article:
                    label = section.get("label") or "Abstract"
                    section_text = safe_value(section.get("text"))
                    article_content_parts.append(f"- {label}: {section_text}")
                article_content_parts.append("")
            else:
                abstract_text = safe_value(row["abstract"])
                if abstract_text and abstract_text != "N/A":
                    article_content_parts.append(
                        "Abstract: " + KnowledgeRepository.strip_leading_label(abstract_text, "Abstract")
                    )
                    article_content_parts.append("")
                else:
                    article_content_parts.append("Abstract: N/A")
                    article_content_parts.append("")

            entities_source = row.get("entities_abstract", [])
            entities = list(entities_source) if isinstance(entities_source, (list, tuple)) else []
            if entities:
                entities = [e.strip() for e in entities if isinstance(e, str) and e.strip()]
                entities = KnowledgeRepository.dedupe_preserve_order(entities)
                article_content_parts.append(f"Entities: {', '.join(entities)}")
                article_content_parts.append("")

            keywords = row.get("keywords", [])
            if isinstance(keywords, (list, tuple)) and keywords:
                kw_list = [kw.strip() for kw in keywords if isinstance(kw, str) and kw.strip()]
                kw_list = KnowledgeRepository.dedupe_preserve_order(kw_list)
                if kw_list:
                    article_content_parts.append(f"Keywords: {', '.join(kw_list)}")
                    article_content_parts.append("")

            references = row.get("references", [])
            if isinstance(references, list) and references:
                article_content_parts.append("References:")
                for ref in references[:10]:
                    citation = safe_value(ref.get("citation"))
                    article_content_parts.append(f"- {citation}")
                article_content_parts.append("")

            article_content = "\n".join(article_content_parts)

            article_metadata = {
                "pmid": pmid,
                "query": query,
                "title": safe_value(row["title"])[:500],
                "journal": safe_value(row.get("journal", "")),
                "type": "pubmed_article",
                "source": "pubmed",
            }

            title_slug = re.sub(r"[^A-Za-z0-9]+", "_", safe_value(row["title"]))[:50]
            article_filename = f"PMID_{pmid}_{title_slug}.txt"

            try:
                file_record = await KnowledgeRepository.upload_report_file(
                    request=request,
                    user=user,
                    filename=article_filename,
                    content=article_content,
                    metadata=article_metadata,
                )
                await KnowledgeRepository.attach_file_to_knowledge(
                    request=request,
                    user=user,
                    kb_id=kb.id,
                    file_id=file_record["id"],
                    content=article_content,
                )

                stored_article = {
                    "pmid": pmid,
                    "title": safe_value(row["title"]),
                    "authors": safe_value(row["authors"]),
                    "doi": safe_value(row["doi"]),
                }

                if self.valves.enable_figure_download:
                    article_pmcid = row.get("pmcid")
                    article_figures = row.get("figures", [])
                    if article_pmcid and article_figures:
                        max_figs = self.valves.max_figures_per_article
                        fig_hrefs = []
                        fig_meta_by_stem: Dict[str, Dict[str, str]] = {}
                        for fig in article_figures:
                            url = fig.get("url", "")
                            if url:
                                basename = url.rsplit("/", 1)[-1] if "/" in url else url
                                fig_hrefs.append(basename)
                                stem = basename.rsplit(".", 1)[0].lower()
                                fig_meta_by_stem[stem] = fig

                        downloaded_images = KnowledgeRepository.download_pmc_figures(
                            pmcid=article_pmcid,
                            figure_hrefs=fig_hrefs,
                            max_figures=max_figs,
                        )
                        fig_stored = 0
                        for img in downloaded_images:
                            try:
                                img_stem = img["filename"].rsplit(".", 1)[0].lower()
                                matched_fig = fig_meta_by_stem.get(img_stem, {})
                                fig_stored += 1
                                fig_filename = f"PMID_{pmid}_fig{fig_stored}.{img['filename'].rsplit('.', 1)[-1]}"
                                fig_metadata = {
                                    "pmid": pmid,
                                    "pmcid": safe_value(article_pmcid),
                                    "figure_index": fig_stored,
                                    "figure_label": safe_value(matched_fig.get("label", "")),
                                    "figure_caption": safe_value(matched_fig.get("caption", ""))[:500],
                                    "original_filename": img["filename"],
                                    "article_title": safe_value(row["title"])[:300],
                                    "query": query,
                                    "type": "pubmed_figure",
                                    "source": "pubmed",
                                }
                                await KnowledgeRepository.upload_image_file(
                                    request=request,
                                    user=user,
                                    filename=fig_filename,
                                    image_data=img["data"],
                                    content_type=img["content_type"],
                                    metadata=fig_metadata,
                                )
                            except Exception as fig_err:
                                logger.warning("Error storing figure for PMID %s: %s", pmid, fig_err)
                        if fig_stored > 0:
                            stored_article["figures_stored"] = fig_stored

                stored_articles.append(stored_article)
                email_lines.append(f"PMID {pmid}: {stored_article['title']}")

                if idx % 5 == 0 or idx == len(new_pmids):
                    await emitter.progress_update(f"📥 Archived {idx}/{len(new_pmids)} article(s) for '{query}'...")

            except Exception as upload_error:
                logger.error("Error uploading PMID %s: %s", pmid, upload_error)
                await emitter.progress_update(f"⚠️ Warning: Failed to archive PMID {pmid}")
                continue

        debug_header = ""
        if self.valves.enable_debug_output:
            debug_header = (
                f"🔧 Query: '{query}' | KB: '{self.valves.default_knowledge_base}' | "
                f"Existing PMIDs: {len(existing_pmids)} | New: {len(stored_articles)}\n\n"
            )

        if stored_articles:
            summaries = [f"- PMID {a['pmid']}: {a['title'][:100]}" for a in stored_articles[:output_limit]]
            extra_count = len(stored_articles) - len(summaries)
            total_figs = sum(a.get("figures_stored", 0) for a in stored_articles)
            fig_summary = f", {total_figs} figure(s) stored" if total_figs else ""
            summary_text = (
                f"🔬 **PubMed Ingest** — {query}\n\n"
                f"{debug_header}"
                f"Archived {len(stored_articles)} new article(s) to '{kb.name}'{fig_summary}:\n"
                + "\n".join(summaries)
            )
            if extra_count > 0:
                summary_text += f"\n... and {extra_count} more (in KB for future queries)"
        else:
            summary_text = (
                f"🔬 **PubMed Ingest** — {query}\n\n"
                f"{debug_header}"
                "No new articles were archived (all fetched articles were already in the knowledge base)."
            )

        return summary_text, len(stored_articles), email_lines


class KnowledgeRepository:
    """Helper for resolving OpenWebUI knowledge bases without exposing extra pipe methods."""

    @staticmethod
    async def load_by_user(user_id: Any, permission: str = "write") -> List[KnowledgeUserModel]:
        knowledge = await Knowledges.get_knowledge_bases_by_user_id(user_id, permission)
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

        by_name = {(kb.name or "").strip().lower(): kb for kb in knowledge_bases if kb.name}
        return by_name.get(normalized)

    @staticmethod
    async def create_knowledge_base(user_id: Any, name: str, description: str = "") -> KnowledgeUserModel:
        """Create a new knowledge base for the user."""
        from open_webui.models.knowledge import KnowledgeForm

        knowledge_form = KnowledgeForm(name=name, description=description, data={})
        kb = await Knowledges.insert_new_knowledge(user_id, knowledge_form)
        if not kb:
            raise ValueError(f"Failed to create knowledge base '{name}'")
        return kb

    @staticmethod
    async def get_or_create_knowledge_base(user_id: Any, name: str, description: str = "") -> KnowledgeUserModel:
        """Find a knowledge base by name, creating it if it doesn't exist.

        Wrapped in a Postgres advisory lock keyed by (user_id, name) so that
        concurrent invocations (e.g. multiple queries dispatched in parallel)
        cannot both see "not found" and each create a duplicate KB.
        """
        async with get_async_db() as lock_db:
            lock_key = f"pubmed_kb:{user_id}:{name}"
            bind = lock_db.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                await lock_db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": lock_key},
                )

            kb = await KnowledgeRepository.find_by_name(user_id, name, permission="write")
            if kb:
                return kb

            return await KnowledgeRepository.create_knowledge_base(user_id=user_id, name=name, description=description)

    # -------- Generic parsing helpers -------- #
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

    # -------- Medline / PMC parsing & figure helpers -------- #
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
        for abstract_elem in article.findall(".//Abstract/AbstractText"):
            label = abstract_elem.attrib.get("Label") or abstract_elem.attrib.get("NlmCategory")
            section_text = KnowledgeRepository.extract_text(abstract_elem)
            abstract_sections.append({"label": label or "Abstract", "text": section_text})

        if abstract_sections:
            combined_sections = []
            for section in abstract_sections:
                sec_label = section.get("label") or "Abstract"
                sec_text = section.get("text") or ""
                combined_sections.append(f"{sec_label}: {sec_text}" if sec_text else sec_label)
            details["abstract"] = "\n".join([part.strip() for part in combined_sections if part]).strip()
        else:
            abstract_elem = article.find(".//AbstractText")
            if abstract_elem is not None:
                full_text = KnowledgeRepository.extract_text(abstract_elem)
                details["abstract"] = full_text

                if "<b>" in ET.tostring(abstract_elem, encoding="unicode", method="xml"):
                    inline_sections = []
                    current_label = None
                    current_text: List[str] = []

                    for child in abstract_elem:
                        if child.tag == "b":
                            if current_label and current_text:
                                inline_sections.append(
                                    {"label": current_label.rstrip(":"), "text": " ".join(current_text).strip()}
                                )
                                current_text = []
                            current_label = (child.text or "").strip()
                            if child.tail:
                                current_text.append(child.tail.strip())
                        elif current_label:
                            if child.text:
                                current_text.append(child.text.strip())
                            if child.tail:
                                current_text.append(child.tail.strip())

                    if current_label and current_text:
                        inline_sections.append(
                            {"label": current_label.rstrip(":"), "text": " ".join(current_text).strip()}
                        )

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

        pub_types = [
            KnowledgeRepository.extract_text(pt)
            for pt in article.findall(".//PublicationType")
            if KnowledgeRepository.extract_text(pt)
        ]
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
            label_elem = next(
                (child for child in fig if KnowledgeRepository.strip_namespace(child.tag) == "label"), None
            )
            caption_elem = next(
                (child for child in fig if KnowledgeRepository.strip_namespace(child.tag) == "caption"), None
            )
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
            figures.append(
                {
                    "label": KnowledgeRepository.extract_text(label_elem) or fig.attrib.get("id", ""),
                    "caption": KnowledgeRepository.extract_text(caption_elem),
                    "url": figure_url,
                }
            )
        return [fig for fig in figures if fig.get("url")]

    @staticmethod
    def download_pmc_figures(
        pmcid: Optional[str],
        figure_hrefs: Optional[List[str]] = None,
        max_figures: int = 0,
    ) -> List[Dict[str, Any]]:
        """Download figure images from a PMC article via the OA tarball."""
        if not pmcid:
            return []

        oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
        try:
            resp = requests.get(oa_url, timeout=15)
            if resp.status_code != 200:
                return []
            oa_root = ET.fromstring(resp.content)
        except Exception:
            return []

        tgz_url = None
        for link in oa_root.iter("link"):
            if link.attrib.get("format") == "tgz":
                href = link.attrib.get("href", "")
                tgz_url = href.replace("ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov")
                break
        if not tgz_url:
            return []

        try:
            resp = requests.get(tgz_url, timeout=120)
            if resp.status_code != 200:
                return []
        except Exception:
            return []

        IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff")
        CONTENT_TYPE_MAP = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }

        href_stems = set()
        if figure_hrefs:
            for h in figure_hrefs:
                stem = h.rsplit(".", 1)[0] if "." in h else h
                href_stems.add(stem.lower())

        results: List[Dict[str, Any]] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    name_lower = member.name.lower()
                    basename = member.name.rsplit("/", 1)[-1] if "/" in member.name else member.name
                    if not any(name_lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
                        continue

                    if href_stems:
                        stem = basename.rsplit(".", 1)[0].lower()
                        if stem not in href_stems:
                            continue

                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    data = f.read()
                    if len(data) < 100:
                        continue

                    ext = "." + basename.rsplit(".", 1)[-1].lower() if "." in basename else ".jpg"
                    content_type = CONTENT_TYPE_MAP.get(ext, "image/jpeg")

                    results.append({"filename": basename, "data": data, "content_type": content_type})

                    if max_figures > 0 and len(results) >= max_figures:
                        break
        except Exception:
            return results

        seen_stems: Dict[str, int] = {}
        for i, r in enumerate(results):
            stem = r["filename"].rsplit(".", 1)[0].lower()
            if stem in seen_stems:
                prev_idx = seen_stems[stem]
                prev_ct = results[prev_idx]["content_type"]
                curr_ct = r["content_type"]
                if prev_ct == "image/gif" and curr_ct in ("image/jpeg", "image/png"):
                    results[prev_idx] = None  # type: ignore[assignment]
                    seen_stems[stem] = i
                else:
                    results[i] = None  # type: ignore[assignment]
            else:
                seen_stems[stem] = i
        results = [r for r in results if r is not None]

        return results

    @staticmethod
    async def upload_image_file(
        request: Any,
        user: Any,
        filename: str,
        image_data: bytes,
        content_type: str,
        metadata: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Upload a binary image to OpenWebUI's file store (not knowledge base)."""
        safe_metadata = metadata.copy() if metadata else {}
        safe_metadata.setdefault("source", "pubmed_ingest_pipe")
        safe_metadata.setdefault("type", "figure")

        final_metadata = {}
        for k, v in safe_metadata.items():
            if v is None:
                continue
            final_metadata[k] = v if isinstance(v, (str, int, float, bool)) else str(v)

        upload = UploadFile(
            filename=filename,
            file=SpooledTemporaryFile(max_size=10 * 1024 * 1024),
            headers={"content-type": content_type},
        )
        upload.file.write(image_data)
        upload.file.seek(0)
        try:
            result = await upload_file_handler(request, upload, final_metadata, False, False, user, None)
        finally:
            await upload.close()

        file_id = getattr(result, "id", None)
        if file_id is None and isinstance(result, dict):
            file_id = result.get("id")
        if not file_id:
            raise ValueError("Failed to upload image into OpenWebUI files")

        if hasattr(result, "model_dump"):
            return result.model_dump()
        if hasattr(result, "dict"):
            return result.dict()
        return result

    # -------- Context resolution helpers -------- #
    @staticmethod
    async def resolve_user(__user__: Optional[dict]) -> Any:
        if not __user__ or not __user__.get("id"):
            raise ValueError("User context with an 'id' is required")
        user = await Users.get_user_by_id(str(__user__["id"]))
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
            params = {"db": "pubmed", "term": query, "api_key": api_key or None}
            response = requests.get(f"{base_url}/espell.fcgi", params=params, timeout=10)
            if response.status_code == 200:
                root = ET.fromstring(response.content)
                corrected = root.find("CorrectedQuery")
                if corrected is not None and corrected.text:
                    suggested = corrected.text.strip()
                    if suggested.lower() != query.lower():
                        return suggested
        except Exception:
            pass
        return None

    @staticmethod
    def generate_query_variations(query: str) -> List[str]:
        """Generate variations of the search query using spaCy and simple heuristics."""
        variations = []
        doc = nlp(query.lower())

        date_pattern = r"\b(19|20)\d{2}\b|\b(\d{4}):(\d{4})\b"
        if re.search(date_pattern, query):
            variation = re.sub(date_pattern, "", query).strip()
            variation = re.sub(r"\s+", " ", variation)
            if variation and variation.lower() != query.lower():
                variations.append(variation)

        field_pattern = r"\[\w+\]"
        if re.search(field_pattern, query):
            variation = re.sub(field_pattern, "", query).strip()
            variation = re.sub(r"\s+", " ", variation)
            if variation and variation.lower() != query.lower():
                variations.append(variation)

        if " AND " in query.upper() or " OR " in query.upper():
            variation = re.sub(r"\s+AND\s+", " OR ", query, flags=re.IGNORECASE)
            if variation.lower() != query.lower():
                variations.append(variation)

            variation = re.sub(r"\s+AND\s+", " ", query, flags=re.IGNORECASE)
            variation = re.sub(r"\s+", " ", variation).strip()
            if variation and variation.lower() != query.lower():
                variations.append(variation)

        entities = [ent.text for ent in doc.ents if ent.label_ in ["DISEASE", "CHEMICAL", "ORG", "GPE"]]
        noun_chunks = [chunk.text for chunk in doc.noun_chunks]

        if entities or noun_chunks:
            variation = " ".join(entities) if entities else None
            if variation and variation.lower() != query.lower():
                variations.append(variation)

            if 0 < len(noun_chunks) <= 3:
                variation = " ".join(noun_chunks)
                if variation.lower() != query.lower():
                    variations.append(variation)

        seen = {query.lower()}
        unique_variations = []
        for var in variations:
            var_lower = var.lower().strip()
            if var_lower and var_lower not in seen and len(var_lower) > 3:
                seen.add(var_lower)
                unique_variations.append(var)

        return unique_variations[:4]

    # -------- Knowledge base interaction wrappers -------- #
    @staticmethod
    async def get_existing_pmids_from_kb(kb_id: str) -> Set[str]:
        """Get all PMIDs already stored in the knowledge base.

        Reads the PMID from the correct nested metadata location
        (`file.meta["data"]["pmid"]` -- see `upload_file_handler` in
        open_webui/routers/files.py, which nests any custom metadata dict
        under `meta["data"]`, never at the top level). Falls back to parsing
        the PMID directly out of the filename (format:
        "PMID_<pmid>_<title_slug>.txt") for resilience against files that
        predate this pipe or came from elsewhere.
        """
        pmids: Set[str] = set()
        try:
            files = await Knowledges.get_files_by_id(kb_id)
            for file_record in files:
                file_meta = getattr(file_record, "meta", None) or {}
                custom_data = file_meta.get("data") or {}
                pmid = custom_data.get("pmid") or file_meta.get("pmid")
                if pmid:
                    pmids.add(str(pmid))
                    continue

                filename = getattr(file_record, "filename", "") or ""
                match = re.match(r"PMID_(\d+)_", filename)
                if match:
                    pmids.add(match.group(1))
        except Exception:
            pass
        return pmids

    @staticmethod
    async def upload_report_file(
        request: Any,
        user: Any,
        filename: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> Dict[str, Any]:
        safe_metadata = metadata.copy() if metadata else {}
        safe_metadata.setdefault("source", "pubmed_ingest_pipe")
        safe_metadata.setdefault("type", "text")

        final_metadata = {}
        for k, v in safe_metadata.items():
            if v is None:
                continue
            final_metadata[k] = v if isinstance(v, (str, int, float, bool)) else str(v)

        upload = UploadFile(
            filename=filename,
            file=SpooledTemporaryFile(max_size=1024 * 1024),
            headers={"content-type": "text/plain"},
        )
        upload.file.write(content.encode("utf-8"))
        upload.file.seek(0)
        try:
            result = await upload_file_handler(request, upload, final_metadata, False, False, user, None)
        finally:
            await upload.close()

        file_id = getattr(result, "id", None)
        if file_id is None and isinstance(result, dict):
            file_id = result.get("id")
        if not file_id:
            raise ValueError("Failed to upload report content into OpenWebUI files")

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
        try:
            await Knowledges.add_file_to_knowledge_by_id(kb_id, file_id, user.id)
        except AttributeError:
            async def _update_metadata() -> bool:
                knowledge = await Knowledges.get_knowledge_by_id(id=kb_id)
                if not knowledge:
                    return False
                data = getattr(knowledge, "data", None) or {}
                file_ids = data.get("file_ids", [])
                if file_id not in file_ids:
                    file_ids.append(file_id)
                    data["file_ids"] = file_ids
                    await Knowledges.update_knowledge_data_by_id(id=kb_id, data=data)
                return True

            updated = await _update_metadata()
            if not updated:
                raise ValueError("Failed to update knowledge metadata with new file")

        async with get_async_db() as db:
            await process_file(
                request,
                ProcessFileForm(file_id=file_id, collection_name=kb_id, content=content),
                user,
                db,
            )
