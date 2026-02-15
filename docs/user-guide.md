# PubMed Deep Research Tool v2.1 - User Guide

## Overview

The PubMed Deep Research Tool integrates PubMed's vast medical literature database directly into OpenWebUI with intelligent knowledge base management, deduplication, and automatic query optimization.

### What's New in v2.1

v2.1 introduces significant improvements over previous versions focused on **token efficiency** and **reliable deduplication**:

- **No context duplication** — Existing knowledge base records are no longer echoed back in tool output. OpenWebUI automatically injects relevant RAG context into the chat, so the tool only returns *new* information. This dramatically reduces token usage on repeated queries.
- **Per-article file storage** — Each article is stored as an individual file (`PMID_{id}_{title}.txt`) with structured metadata, replacing the single combined report file. This enables precise deduplication and cleaner knowledge base management.
- **Dual PMID deduplication** — Combines file metadata scanning (reliable) with RAG text search (fallback) for robust duplicate detection. Previous versions relied solely on text matching.
- **Smart abstract truncation** — Abstracts in LLM output are truncated to 1,500 characters with a note that the full text is available in the knowledge base, keeping responses concise.
- **Decoupled archival and output** — All fetched articles are archived to the knowledge base regardless of `reranker_results`. The reranker only limits what's shown to the LLM, not what's stored.
- **Enriched hybrid search** — New `enable_enriched_hybrid_search` valve adds document metadata to BM25 text for improved keyword matching.
- **Resilient archival** — Individual article upload failures are logged and skipped instead of aborting the entire batch.
- **Compact archive summaries** — New articles are listed as a brief summary (PMID + title) instead of repeating full content already stored in the KB.

## What to Expect

### First Use
When you first use the tool with a new query:

1. **Knowledge Base Auto-Creation** - If the configured knowledge base doesn't exist, it's created automatically
2. **PubMed Search** - Searches PubMed for articles matching your query
3. **Smart Processing** - Processes articles with NLP to extract entities, keywords, and structured data
4. **Per-Article Storage** - Each article is stored as an individual file with PMID metadata for precise deduplication
5. **Concise Results** - Returns article details with truncated abstracts (full text stored in KB for RAG retrieval)

### Subsequent Uses
When you search the same or similar topics again:

1. **Dual Duplicate Detection** - Checks file metadata for stored PMIDs *and* scans RAG text as fallback
2. **Smart Fetching** - Fetches extra articles to account for duplicates (configurable multiplier)
3. **New Articles Only** - Filters out existing articles and only processes/stores new ones
4. **Incremental Updates** - Your knowledge base grows over time without redundant data
5. **No Context Duplication** - Previously stored articles are NOT re-sent to the LLM. OpenWebUI's RAG automatically injects relevant prior knowledge into the conversation context, so the tool only returns new findings

### Query Variation (Auto-Retry)
If no new articles are found, the tool automatically:

1. **Spell Check** - Uses PubMed's spell checker to fix typos
2. **Query Broadening** - Removes restrictive terms (dates, field tags, Boolean operators)
3. **Entity Extraction** - Tries searching with just key medical/scientific terms
4. **Progress Updates** - Shows each variation attempt transparently

---

## Configuration (Valves)

The tool is highly configurable through "valves" in OpenWebUI's tool settings. Here's what each valve does:

### API & Connection Settings

#### `pubmed_base_url`
- **Type**: String
- **Default**: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils`
- **Description**: PubMed E-utilities base URL
- **When to change**: Only if NCBI changes their API endpoint (rare)

#### `pubmed_api_key`
- **Type**: String
- **Default**: Empty
- **Description**: Optional NCBI API key for higher rate limits
- **Why use it**: 
  - Without key: 3 requests/second
  - With key: 10 requests/second
- **How to get**: Register at https://www.ncbi.nlm.nih.gov/account/

---

### Knowledge Base Settings

#### `default_knowledge_base`
- **Type**: String
- **Default**: `Pubmed Knowledge Base`
- **Description**: Default knowledge base name to search/store data
- **Important**: Will be created automatically if it doesn't exist
- **Use case**: Set different knowledge bases for different research projects

---

### Search & Retrieval Settings

#### `max_results`
- **Type**: Integer
- **Default**: `10`
- **Description**: Maximum number of **NEW** results to retrieve (excludes already-stored articles)
- **Range**: 1-100 recommended
- **Use case**: 
  - Small values (5-10): Quick updates
  - Large values (50-100): Comprehensive research

#### `fetch_multiplier`
- **Type**: Float
- **Default**: `2.5`
- **Description**: Multiplier for initial fetch to account for duplicates
- **How it works**: If you want 10 new articles, it fetches 25 (10 × 2.5) then filters
- **When to adjust**:
  - Set to `1.0`: Disable optimization (fetch exactly max_results)
  - Set higher (3.0-5.0): If you have many existing articles
  - Set lower (1.5-2.0): For faster searches on new topics

---

### Hybrid Search Settings

#### `enable_hybrid_search`
- **Type**: Boolean
- **Default**: `False`
- **Description**: Enable hybrid search (semantic + keyword)
- **⚠️ Important**: 
  - **Global setting must be ON** in OpenWebUI first
  - If global is OFF, this valve has no effect
  - If global is ON, setting this to False disables hybrid for this tool only
- **Use case**: Better retrieval when you know exact terminology

#### `hybrid_bm25_weight`
- **Type**: Float (0.0-1.0)
- **Default**: `0.5`
- **Description**: Balance between keyword (BM25) and semantic search
- **How it works**:
  - **0.0-0.3**: Favor semantic similarity (meaning-based)
  - **0.5**: Balanced approach (recommended)
  - **0.7-1.0**: Favor keyword matching (exact terms)
- **Only applies when**: Hybrid search is enabled

#### `enable_enriched_hybrid_search`
- **Type**: Boolean
- **Default**: `False`
- **Description**: Enrich BM25 text for hybrid search with document metadata (filename, title, headings, source)
- **How it works**: Adds contextual metadata to the keyword search text, improving BM25 matching accuracy
- **Only applies when**: Hybrid search is enabled
- **⚠️ Note**: The global "Enable Enriched Hybrid Search Texts" setting in OpenWebUI Admin → Settings → Documents takes precedence if set
- **Use case**: When your queries include terms that appear in filenames or titles but not always in article body text

---

### Reranking & Filtering Settings

#### `reranker_results`
- **Type**: Integer
- **Default**: `0`
- **Description**: Number of results to return to the LLM after reranking (0 disables reranking and returns `max_results`)
- **Important**: All `max_results` articles are still fetched and **archived to the knowledge base**. This valve only limits what's included in the LLM response to stay within context limits.
- **Use case**: Set to top-N (e.g., 5) to get the most relevant results while still archiving everything
- **Requires**: Reranker configured in OpenWebUI

#### `relevance_threshold`
- **Type**: Float (0.0-1.0)
- **Default**: `0.0`
- **Description**: Minimum relevance score threshold
- **How it works**:
  - `0.0`: Accept all results
  - `0.5`: Only moderately relevant
  - `0.8+`: Only highly relevant
- **Use case**: Filter out low-quality matches

---

### Query Variation Settings

#### `enable_query_variation`
- **Type**: Boolean
- **Default**: `True`
- **Description**: Automatically try alternative queries if no new results found
- **Strategies used**:
  1. PubMed spell check
  2. Remove date constraints
  3. Remove field tags (e.g., `[title]`, `[author]`)
  4. Broaden Boolean operators
  5. Extract key entities
- **When to disable**: If you want exact query matches only

#### `max_query_attempts`
- **Type**: Integer (1-5)
- **Default**: `3`
- **Description**: Maximum number of query variations to try
- **Trade-off**:
  - **Lower (1-2)**: Faster, less API usage
  - **Higher (4-5)**: Better chance of finding results
- **Note**: Only applies if `enable_query_variation` is True

---

### Debug Settings

#### `enable_debug_output`
- **Type**: Boolean
- **Default**: `True`
- **Description**: Include debug information in responses
- **Shows**:
  - Query details
  - Knowledge base name
  - Max results, reranker settings
  - Hybrid search status
- **When to disable**: For cleaner output to end users

---

## Typical Workflows

### Quick Research Update
```yaml
max_results: 5
fetch_multiplier: 2.0
enable_query_variation: true
max_query_attempts: 2
```

### Deep Comprehensive Research
```yaml
max_results: 50
fetch_multiplier: 3.0
enable_query_variation: true
max_query_attempts: 5
enable_hybrid_search: true  # If global is on
hybrid_bm25_weight: 0.5
```

### Exact Query Matching Only
```yaml
max_results: 10
fetch_multiplier: 1.0
enable_query_variation: false
enable_hybrid_search: true
hybrid_bm25_weight: 0.9  # Favor keywords
```

### High-Precision Results Only
```yaml
max_results: 10
reranker_results: 5
relevance_threshold: 0.7
enable_query_variation: true
```

---

## Understanding the Output

### Progress Messages

| Message | Meaning |
|---------|---------|
| 🔬 Initializing PubMed deep research... | Starting the tool |
| ⚙️ Settings: Max Results: 10... | Shows configured settings |
| 📦 Creating new knowledge base: ... | Creating KB (first time) |
| 🔍 Querying knowledge container: ... | Checking existing data |
| 📚 Found N existing articles | N articles already stored (by PMID) |
| 📥 Fetching up to 25 articles... | Using fetch multiplier |
| 🔄 Filtered out 15 existing article(s), 10 new found | Deduplication working |
| ✂️ Limited to 10 new articles | Applied max_results limit |
| 💭 No new results, trying variation 2... | Query variation activated |
| 📝 Trying spell-corrected query: ... | Using spell check |
| 🔀 Trying broadened query: ... | Using query expansion |
| 🆕 Archiving N new article(s)... | Storing new results individually |
| 📥 Archived 5/10 articles... | Progress during batch archival |
| ⚠️ Warning: Failed to archive PMID ... | Individual article upload failed (non-fatal) |
| ✅ Research complete! | Done! |

### Response Sections

v2.1 streamlines output to avoid duplicating content that OpenWebUI's RAG already provides:

1. **Archive Summary** - Compact list of newly archived articles (PMID + truncated title). If no new articles were found, an update notice is shown instead.
2. **Article Details** - Structured details for each article (title, authors, DOI, PMID, truncated abstract, entities, keywords). Limited to `reranker_results` articles if set, otherwise `max_results`.
3. **Omitted Note** - If more articles were archived than shown, a note indicates how many were stored in the KB but excluded from the response.

> **Key difference from v2.0**: Existing knowledge base records and "Updated Knowledge Snapshot" sections have been removed. OpenWebUI automatically injects relevant RAG context from your knowledge base into the conversation, so the tool no longer needs to repeat that content. This saves significant tokens on repeated searches.

---

## Tips & Best Practices

### For Best Results

✅ **Use specific medical terminology** - PubMed understands MeSH terms  
✅ **Start broad, then narrow** - Let query variation help if too specific  
✅ **Enable hybrid search** for technical queries with known terms  
✅ **Use higher fetch multiplier** for well-researched topics  
✅ **Check knowledge base periodically** - Run same queries to get updates  
✅ **Trust the RAG** - Don't worry about missing prior results; OpenWebUI injects relevant KB context automatically  
✅ **Use `reranker_results`** to keep LLM output focused while still archiving everything  

### Avoid Common Mistakes

❌ **Don't disable query variation** unless you know why  
❌ **Don't set fetch_multiplier too low** (<1.5) if you have existing data  
❌ **Don't enable hybrid search** if the global setting is off  
❌ **Don't use very high max_results** (>100) - slow and may hit API limits  

### API Rate Limits

Without API key:
- 3 requests/second
- Recommended: max_results ≤ 20

With API key:
- 10 requests/second
- Recommended: max_results ≤ 50

---

## Troubleshooting

### "Knowledge container not found"
- **Cause**: Knowledge base name doesn't exist and auto-create failed
- **Fix**: Check permissions, try different name, or create manually in OpenWebUI

### "No new articles found. All fetched articles are already in the knowledge base."
- **Normal**: You're up-to-date on this topic! Your existing articles are still available via RAG.
- **If unexpected**: Try increasing `fetch_multiplier` or check if query is too specific
- **Note**: Even when no *new* articles are found, OpenWebUI will still inject relevant existing KB articles into the conversation context

### "Hybrid search not working"
- **Check**: Is hybrid search enabled **globally** in OpenWebUI Admin → Settings → Documents?
- **If no**: Enable global setting first
- **If yes**: Set `enable_hybrid_search: true` in valve

### Slow performance
- **Reduce** `max_results` (each article requires multiple API calls)
- **Consider** getting an NCBI API key for faster limits
- **Disable** `enable_query_variation` if not needed
- **Lower** `max_query_attempts`

### Getting irrelevant results
- **Increase** `relevance_threshold` (e.g., 0.6 or 0.7)
- **Enable** `reranker_results` with a small number (e.g., 5)
- **Adjust** `hybrid_bm25_weight` toward 1.0 for keyword matching
- **Disable** `enable_query_variation` to avoid query broadening

---

## Examples

### Example 1: Researching a new disease
```
Query: "long COVID neurological symptoms"
Expected: Full articles, entities extracted, stored for RAG
Output: 10 new articles with abstracts, keywords, references
```

### Example 2: Updating existing research
```
Query: "breast cancer immunotherapy" (searched before)
Expected: Some duplicates filtered, only new articles added
Output: "Filtered out 8 existing articles, 2 new found"
```

### Example 3: Typo in query
```
Query: "diabeetes treatment"
Expected: Spell check fixes to "diabetes treatment"
Output: "Trying spell-corrected query: 'diabetes treatment'"
```

### Example 4: Too-specific query with auto-recovery
```
Query: "COVID-19 treatment hydroxychloroquine 2020[pdat]"
Attempt 1: No new results
Attempt 2: Removes date → "COVID-19 treatment hydroxychloroquine"
Output: "Trying broadened query: ..." → Finds results
```

---

## Advanced Usage

### Multiple Knowledge Bases
Create separate knowledge bases for different projects:
```yaml
# Project 1: Cancer Research
default_knowledge_base: "Cancer Research KB"

# Project 2: Cardiology
default_knowledge_base: "Cardiology Studies KB"
```

### Custom Workflows
Combine with OpenWebUI functions for:
- Automated literature reviews
- Citation tracking
- Research summarization
- Evidence synthesis

---

## Support & Resources

- **PubMed Help**: https://pubmed.ncbi.nlm.nih.gov/help/
- **NCBI API Key**: https://www.ncbi.nlm.nih.gov/account/
- **OpenWebUI Docs**: https://docs.openwebui.com/
- **Tool Issues**: Check GitHub repository for updates

---

## Version Information

**Tool Version**: 2.1 (v2.2.0)  
**Features**:
- ✅ Native OpenWebUI integration
- ✅ Automatic knowledge base creation
- ✅ Per-article file storage with PMID metadata
- ✅ Dual deduplication (file metadata + RAG text)
- ✅ No context duplication — token-efficient output
- ✅ Smart abstract truncation (full text in KB)
- ✅ Decoupled archival and LLM output
- ✅ NLP processing (spaCy, NLTK)
- ✅ Automatic query variation with spell check
- ✅ Hybrid search with enriched BM25 support
- ✅ RAG-enhanced retrieval
- ✅ Resilient per-article error handling

---

## Changelog

### v2.1 (v2.2.0)
- **No context duplication**: Removed "Existing Knowledge Base Records" and "Updated Knowledge Snapshot" sections from tool output. OpenWebUI's RAG handles injecting prior knowledge automatically.
- **Per-article storage**: Each article is now stored as an individual file (`PMID_{id}_{title}.txt`) with metadata including PMID, query, title, journal, type, and source.
- **Dual PMID deduplication**: File metadata scanning for reliable PMID lookup, with RAG text search as fallback.
- **Abstract truncation**: LLM-facing output truncates abstracts to 1,500 characters; full text is always available in the KB.
- **Decoupled reranker**: `reranker_results` now only limits LLM output — all articles are still archived to the KB for future queries.
- **New valve: `enable_enriched_hybrid_search`**: Enriches BM25 text with document metadata for better keyword matching.
- **Resilient archival**: Individual article upload failures are caught and logged; remaining articles continue processing.
- **Compact archive summaries**: New articles shown as PMID + truncated title instead of repeating full content.
- **Structured abstract sections**: Better handling of structured abstracts with inline `<b>` tagged sections and section deduplication.

### v2.0
- Initial release with native OpenWebUI integration
- Knowledge base auto-creation
- Smart deduplication via text search
- NLP processing (spaCy, NLTK)
- Automatic query variation
- Hybrid search support
- RAG-enhanced retrieval
