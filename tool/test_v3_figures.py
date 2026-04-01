#!/usr/bin/env python3
"""
Standalone test for v3 figure download features.
Tests fetch_pmc_figures() and download_pmc_figures() (OA tarball) against live PMC data
without requiring OpenWebUI.
"""

import io
import os
import re
import requests
import sys
import tarfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple


# --- Extracted from v3: figure-related static methods ---

def extract_text(elem) -> str:
    if elem is None:
        return ""
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(extract_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts).strip()


def fetch_pmc_figures(pmcid: Optional[str], api_key: str = "") -> List[Dict[str, str]]:
    """Fetch figure metadata from a PMC article XML."""
    if not pmcid:
        return []
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    params = {"db": "pmc", "id": pmcid, "retmode": "xml"}
    if api_key:
        params["api_key"] = api_key
    try:
        response = requests.get(f"{base_url}/efetch.fcgi", params=params, timeout=15)
        if response.status_code != 200:
            return []
    except Exception as e:
        print(f"  [WARN] PMC fetch failed: {e}")
        return []

    root = ET.fromstring(response.content)
    figures: List[Dict[str, str]] = []
    for fig in root.iter("fig"):
        label_elem = fig.find("label")
        caption_elem = fig.find("caption")
        graphic_elem = fig.find("graphic")
        if graphic_elem is None:
            graphic_elem = fig.find("./alternatives/graphic")
            if graphic_elem is None:
                for child in fig:
                    if child.tag and "graphic" in child.tag.lower():
                        graphic_elem = child
                        break
        href_value = ""
        figure_url = ""
        if graphic_elem is not None:
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
            "label": extract_text(label_elem) or fig.attrib.get("id", ""),
            "caption": extract_text(caption_elem),
            "url": figure_url,
        })
    return [fig for fig in figures if fig.get("url")]


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
            print(f"  [WARN] OA service returned {resp.status_code}")
            return []
        oa_root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"  [WARN] OA service failed: {e}")
        return []

    tgz_url = None
    for link in oa_root.iter("link"):
        if link.attrib.get("format") == "tgz":
            href = link.attrib.get("href", "")
            tgz_url = href.replace("ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov")
            break

    if not tgz_url:
        print("  [WARN] No tgz link found (article may not be open access)")
        return []

    print(f"  Tarball: {tgz_url}")
    try:
        resp = requests.get(tgz_url, timeout=120)
        if resp.status_code != 200:
            return []
        print(f"  Downloaded {len(resp.content):,} bytes")
    except Exception as e:
        print(f"  [WARN] Tarball download failed: {e}")
        return []

    IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff")
    CONTENT_TYPE_MAP = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".tif": "image/tiff", ".tiff": "image/tiff",
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

                # Skip GIF when JPG/PNG of same figure exists
                if name_lower.endswith(".gif"):
                    stem = basename.rsplit(".", 1)[0]
                    jpg_exists = any(
                        r["filename"].rsplit(".", 1)[0] == stem
                        and r["content_type"] in ("image/jpeg", "image/png")
                        for r in results
                    )
                    if jpg_exists:
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

                results.append({
                    "filename": basename,
                    "data": data,
                    "content_type": content_type,
                })

                if max_figures > 0 and len(results) >= max_figures:
                    break
    except Exception as e:
        print(f"  [WARN] Tarball extraction error: {e}")

    return results


# --- PubMed search helper ---

def search_pubmed_for_pmcid(query: str, max_results: int = 3) -> List[str]:
    """Search PubMed and return PMCIDs."""
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    params = {"db": "pubmed", "term": query, "retmax": max_results * 3, "retmode": "xml"}
    resp = requests.get(f"{base_url}/esearch.fcgi", params=params, timeout=10)
    root = ET.fromstring(resp.content)
    pmids = [id_elem.text for id_elem in root.findall(".//Id") if id_elem.text]

    if not pmids:
        return []

    pmcids = []
    for pmid in pmids[:max_results * 3]:
        params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
        resp = requests.get(f"{base_url}/efetch.fcgi", params=params, timeout=10)
        if resp.status_code != 200:
            continue
        root = ET.fromstring(resp.content)
        for article_id in root.iter("ArticleId"):
            if article_id.attrib.get("IdType") == "pmc":
                pmcids.append(article_id.text)
                break
        if len(pmcids) >= max_results:
            break
    return pmcids


def main():
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "SGLT2 inhibitors heart failure"
    print(f"=== PubMed v3 Figure Test (OA Tarball) ===")
    print(f"Query: {query}\n")

    print("[1] Searching PubMed for articles with PMCIDs...")
    pmcids = search_pubmed_for_pmcid(query, max_results=2)
    if not pmcids:
        print("  No PMC articles found. Try a different query.")
        return
    print(f"  Found PMCIDs: {pmcids}\n")

    output_dir = "/tmp/pubmed_v3_test_figures"
    # Clean previous run
    if os.path.exists(output_dir):
        for f in os.listdir(output_dir):
            os.remove(os.path.join(output_dir, f))
    os.makedirs(output_dir, exist_ok=True)

    total_figures_meta = 0
    total_downloaded = 0

    for pmcid in pmcids:
        print(f"[2] Fetching figure metadata for {pmcid}...")
        figures = fetch_pmc_figures(pmcid)
        print(f"  Found {len(figures)} figure(s) in XML metadata")
        total_figures_meta += len(figures)

        for i, fig in enumerate(figures, 1):
            label = fig.get("label", "?")
            caption = fig.get("caption", "")
            if len(caption) > 80:
                caption = caption[:80] + "..."
            print(f"  [{i}] {label}: {caption}")

        fig_hrefs = []
        for fig in figures:
            url = fig.get("url", "")
            if url:
                basename = url.rsplit("/", 1)[-1] if "/" in url else url
                fig_hrefs.append(basename)

        print(f"\n[3] Downloading images via OA tarball for {pmcid}...")
        images = download_pmc_figures(pmcid, figure_hrefs=fig_hrefs)
        print(f"  Extracted {len(images)} image(s)")

        for img in images:
            total_downloaded += 1
            filepath = os.path.join(output_dir, f"{pmcid}_{img['filename']}")
            with open(filepath, "wb") as f:
                f.write(img["data"])
            print(f"  -> {img['filename']} ({len(img['data']):,} bytes, {img['content_type']})")

        print()

    print(f"=== Results ===")
    print(f"PMC articles checked: {len(pmcids)}")
    print(f"Figure metadata found: {total_figures_meta}")
    print(f"Images downloaded: {total_downloaded}")
    print(f"Output: {output_dir}")

    if total_downloaded > 0:
        print(f"\nSaved files:")
        for f in sorted(os.listdir(output_dir)):
            size = os.path.getsize(os.path.join(output_dir, f))
            print(f"  {f} ({size:,} bytes)")
    
    print(f"\nTest {'PASSED' if total_downloaded > 0 else 'FAILED'}")


if __name__ == "__main__":
    main()
