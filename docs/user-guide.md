# PubMed Deep Research Tool - User Guide

## Overview

The PubMed Deep Research Tool integrates PubMed's vast medical literature database directly into OpenWebUI with intelligent knowledge base management, deduplication, and automatic query optimization.

## What to Expect

### First Use
When you first use the tool with a new query:

1. **Knowledge Base Auto-Creation** - If the configured knowledge base doesn't exist, it's created automatically
2. **PubMed Search** - Searches PubMed for articles matching your query
3. **Smart Processing** - Processes articles with NLP to extract entities, keywords, and structured data
4. **Knowledge Storage** - Embeds processed articles into your knowledge base for future RAG queries
5. **Comprehensive Results** - Returns full article details including abstracts, authors, DOIs, references, and figures

### Subsequent Uses
When you search the same or similar topics again:

1. **Duplicate Detection** - Checks existing knowledge base for PMIDs (PubMed IDs) you already have
2. **Smart Fetching** - Fetches extra articles to account for duplicates (configurable multiplier)
3. **New Articles Only** - Filters out existing articles and only processes/stores new ones
4. **Incremental Updates** - Your knowledge base grows over time without redundant data

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

---

### Reranking & Filtering Settings

#### `reranker_results`
- **Type**: Integer
- **Default**: `0`
- **Description**: Number of results to retain after reranking (0 disables)
- **Use case**: Set to top-N (e.g., 5) to get only the most relevant results
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
| 📚 Found existing knowledge entries | You have related articles stored |
| 📥 Fetching up to 25 articles... | Using fetch multiplier |
| 🔄 Filtered out 15 existing article(s), 10 new found | Deduplication working |
| ✂️ Limited to 10 new articles | Applied max_results limit |
| 💭 No new results, trying variation 2... | Query variation activated |
| 📝 Trying spell-corrected query: ... | Using spell check |
| 🔀 Trying broadened query: ... | Using query expansion |
| 🆕 Found 5 new articles; archiving... | Storing new results |
| ✅ Research complete! | Done! |

### Response Sections

1. **Existing Knowledge Base Records** - Data already in your KB matching the query
2. **New Records Archived** - Summary of newly added articles with basic metadata
3. **Update Notice** - If no new articles were found (all duplicates)
4. **Updated Knowledge Snapshot** - Refreshed RAG results after adding new data
5. **Full Report of Current Search** - Complete details of all articles found in this session (even if some were duplicates)

---

## Tips & Best Practices

### For Best Results

✅ **Use specific medical terminology** - PubMed understands MeSH terms  
✅ **Start broad, then narrow** - Let query variation help if too specific  
✅ **Enable hybrid search** for technical queries with known terms  
✅ **Use higher fetch multiplier** for well-researched topics  
✅ **Check knowledge base periodically** - Run same queries to get updates  

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
- **Normal**: You're up-to-date on this topic!
- **If unexpected**: Try increasing `fetch_multiplier` or check if query is too specific

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

**Tool Version**: 2.0.0  
**Features**:
- ✅ Native OpenWebUI integration
- ✅ Automatic knowledge base creation
- ✅ Smart deduplication
- ✅ NLP processing (spaCy, NLTK)
- ✅ Automatic query variation
- ✅ Hybrid search support
- ✅ RAG-enhanced retrieval
