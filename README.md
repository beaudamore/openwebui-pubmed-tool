# OpenWebUI PubMed Tool

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![OpenWebUI](https://img.shields.io/badge/Open_WebUI-Tool-green)
![PubMed](https://img.shields.io/badge/PubMed-35M+_Articles-red)
![License](https://img.shields.io/badge/License-MIT-yellow)
![NLP](https://img.shields.io/badge/NLP-Entity_Extraction-orange)

Deep research tool integrating PubMed's medical literature database into OpenWebUI with intelligent knowledge base management, automatic deduplication, and NLP-powered article processing.

**Version:** 2.0.0  
**Author:** Beau D'Amore ([www.damore.ai](https://www.damore.ai))

## Features

- **PubMed Integration**: Direct access to 35+ million medical citations and abstracts
- **Intelligent KB Management**: 
  - Automatic knowledge base creation
  - Smart deduplication (tracks PMIDs to avoid duplicates)
  - Incremental KB growth without redundant data
  
- **Smart Query Processing**:
  - Automatic spell correction using PubMed's spell checker
  - Query broadening for better results
  - Medical entity extraction (diseases, drugs, procedures)
  - Automatic retry with query variations

- **Rich Article Data**:
  - Full abstracts and article metadata
  - Author information and affiliations
  - DOI links and PubMed URLs
  - Referenced PMIDs
  - Figure and table information
  - Publication types and keywords

- **NLP Processing**:
  - Entity extraction (diseases, chemicals, genes, procedures)
  - Keyword extraction and TF-IDF analysis
  - Automatic structured data generation
  - Medical concept recognition

- **Advanced Search Features**:
  - Hybrid search (semantic + keyword)
  - Relevance-based reranking
  - Configurable fetch multiplier for duplicate handling
  - Date range filtering
  - Custom result limits

## Installation

### Requirements

```bash
pip install requests pandas spacy nltk
python -m spacy download en_core_web_sm
```

### Setup in OpenWebUI

1. Upload `tool/pubmed_internal_v2.py` to OpenWebUI
2. Configure valves:
   - Set knowledge base name
   - Optionally add NCBI API key for higher rate limits
   - Adjust search and retrieval settings

## Usage

### First Use Workflow

1. **Knowledge Base Auto-Creation**: If KB doesn't exist, it's created automatically
2. **PubMed Search**: Searches PubMed for matching articles
3. **Smart Processing**: Processes articles with NLP
4. **Knowledge Storage**: Embeds processed articles for future RAG queries
5. **Comprehensive Results**: Returns full article details

### Subsequent Uses

1. **Duplicate Detection**: Checks existing KB for PMIDs already stored
2. **Smart Fetching**: Fetches extra articles to account for duplicates
3. **New Articles Only**: Filters and processes only new articles
4. **Incremental Updates**: KB grows without redundant data

### Query Examples

```
"Find recent articles on CRISPR gene editing"
"Search for COVID-19 vaccine efficacy studies from 2023"
"What are the latest treatments for type 2 diabetes?"
"Research on Alzheimer's disease biomarkers"
```

## Configuration (Valves)

### Essential Settings

- **default_knowledge_base**: KB name for storing/searching (default: `Pubmed Knowledge Base`)
- **max_results**: Maximum new results to retrieve (default: 10)
- **pubmed_api_key**: Optional NCBI API key for higher rate limits
- **fetch_multiplier**: Multiplier for initial fetch to account for duplicates (default: 2.5)

### Advanced Settings

- **enable_hybrid_search**: Enable semantic + keyword search
- **reranker_results**: Number of results after reranking
- **relevance_threshold**: Minimum relevance score (0.0-1.0)
- **auto_create_kb**: Automatically create KB if it doesn't exist
- **enable_debug**: Show detailed processing information

See [docs/user-guide.md](docs/user-guide.md) for complete configuration options.

## NCBI API Key

Get higher rate limits (10 requests/second vs 3):
1. Register at https://www.ncbi.nlm.nih.gov/account/
2. Add your API key to the tool settings

## Documentation

- [Complete User Guide](docs/user-guide.md)
- [Setup Instructions](docs/setup.md)
- [Headless scheduled triggering](docs/headless-tool-triggering.md)
- [Physician System Prompt](prompt/pubmed-physician-system-prompt.md)

## License

MIT License - See individual repository for details

## Author

**Beau D'Amore**  
[www.damore.ai](https://www.damore.ai)
