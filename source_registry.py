# -*- coding: utf-8 -*-

from typing import Iterable


CORE_SOURCE_RECORDS = [
    {
        "name": "OpenAlex",
        "url": "https://openalex.org/",
        "purpose": "作者身份、机构、论文、主题与引文指标交叉验证",
        "fetch_for_context": False,
    },
    {
        "name": "Semantic Scholar",
        "url": "https://www.semanticscholar.org/",
        "purpose": "作者身份和代表论文按需交叉验证",
        "fetch_for_context": False,
    },
    {
        "name": "Tavily 网页检索",
        "url": "https://www.tavily.com/",
        "purpose": "发现专家主页、奖项、合作新闻和公开网页证据",
        "fetch_for_context": False,
    },
]


AGRICULTURE_SOURCE_RECORDS = [
    {
        "name": "中国农业科学院农业资源与农业区划研究所杰出人才",
        "url": "https://iarrp.caas.cn/rcdw1/jcrc/gjzrkxjjzdxmzcr/index.htm",
        "purpose": "补充农业领域国内杰出人才候选",
        "fetch_for_context": True,
    },
    {
        "name": "Stanford/Elsevier 全球前 2% 顶尖科学家数据集",
        "url": "https://elsevier.digitalcommonsdata.com/datasets/btchxktzyw/8",
        "purpose": "补充高影响力农业科学家候选与排名证据",
        "fetch_for_context": True,
    },
    {
        "name": "世界粮食奖 2020-2026 获奖者",
        "url": "https://www.worldfoodprize.org/en/laureates/20202026_laureates/",
        "purpose": "补充农业与粮食领域最高奖项获得者",
        "fetch_for_context": True,
    },
    {
        "name": "世界粮食奖 2025 农业食品领军人物",
        "url": "https://www.worldfoodprize.org/index.cfm?nodeID=97060&audienceID=1",
        "purpose": "补充农业食品领域领军人物",
        "fetch_for_context": True,
    },
    {
        "name": "世界粮食奖 2024 农业食品领军人物",
        "url": "https://www.worldfoodprize.org/en/nominations/top_agrifood_pioneers/2024_top_agrifood_pioneers_list/",
        "purpose": "补充农业食品领域领军人物",
        "fetch_for_context": True,
    },
    {
        "name": "CIGR 信息技术分会成员",
        "url": "https://www.cigr.org/SectionVII",
        "purpose": "补充现代农业信息技术与农业工程专家",
        "fetch_for_context": True,
    },
    {
        "name": "CIGR 成员与治理机构名录",
        "url": "https://www.cigr.org/membersxgoverning-bodies-overview",
        "purpose": "交叉验证 CIGR 成员身份与任职",
        "fetch_for_context": True,
    },
]


AGRICULTURE_QUERY_MARKERS = [
    "农业",
    "农学",
    "作物",
    "种植",
    "养殖",
    "粮食",
    "农机",
    "农业工程",
    "农业信息",
    "种质",
    "agriculture",
    "agricultural",
    "crop",
    "farming",
    "food science",
]


def is_agriculture_query(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(marker.lower() in lowered for marker in AGRICULTURE_QUERY_MARKERS)


def _dedupe_sources(records: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    deduped = []
    seen = set()
    for record in records:
        url = str(record.get("url", "")).strip()
        key = url.rstrip("/").lower()
        if url and key not in seen:
            seen.add(key)
            deduped.append(dict(record))
    return deduped


def source_records_for_query(
    query: str = "",
    extra_data_source_urls: list[str] | None = None,
) -> list[dict[str, object]]:
    records = list(CORE_SOURCE_RECORDS)
    if is_agriculture_query(query):
        records.extend(AGRICULTURE_SOURCE_RECORDS)

    for index, url in enumerate(extra_data_source_urls or [], start=1):
        clean_url = str(url).strip()
        if clean_url.startswith(("http://", "https://")):
            records.append(
                {
                    "name": f"用户补充数据源 {index}",
                    "url": clean_url,
                    "purpose": "用户指定的候选专家与公开信息补充来源",
                    "fetch_for_context": True,
                }
            )

    return _dedupe_sources(records)


def fetchable_source_records(
    query: str = "",
    extra_data_source_urls: list[str] | None = None,
) -> list[dict[str, object]]:
    return [
        record
        for record in source_records_for_query(query, extra_data_source_urls)
        if bool(record.get("fetch_for_context"))
    ]
