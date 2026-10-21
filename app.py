from __future__ import annotations

import re
import os
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from statistics import median
from urllib.parse import unquote
from xml.sax.saxutils import escape

import pandas as pd
import requests
import streamlit as st

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except ImportError:  # PDF dependencies are installed by requirements.txt
    colors = None


APP_DIR = Path(__file__).resolve().parent
def load_local_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting process environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_env(APP_DIR / ".env")
SPEC_PATH = APP_DIR / "사업지명세서.txt"
API_URL = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/"
    "getRTMSDataSvcAptTrade"
)
AS_OF = date(2026, 10, 21)
DEFAULT_START = date(2025, 10, 21)
PRICE_PER_PYEONG = 3.305785
PAGE_SIZE = 1000

st.set_page_config(
    page_title="PF Navigator | 사업성 검토",
    page_icon="▦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root { color-scheme: light; }
    .stApp { background:#f3f5f7; color:#17283d; }
    [data-testid="stHeader"] { background:rgba(243,245,247,.96); }
    [data-testid="stSidebar"] { background:#142a43; }
    [data-testid="stSidebar"] * { color:#e9eef3; }
    [data-testid="stSidebar"] [data-baseweb="input"] input,
    [data-testid="stSidebar"] [data-baseweb="select"] * { color:#17283d !important; }
    h1,h2,h3 { color:#142a43; letter-spacing:-.025em; }
    .pf-kicker { color:#718094; font-size:.78rem; letter-spacing:.12em; text-transform:uppercase; }
    .pf-subtitle { color:#68798a; margin-top:-.55rem; font-size:.92rem; }
    .pf-section { background:#fff; border:1px solid #dce3e9; padding:1rem 1.1rem; margin:.35rem 0 1rem; }
    .pf-section-title { display:flex; align-items:center; gap:.6rem; color:#142a43; font-weight:650; font-size:1.04rem; }
    .pf-index { color:#98783e; font-size:.75rem; letter-spacing:.08em; }
    .pf-caption { color:#68798a; font-size:.79rem; }
    .pf-pill { display:inline-block; padding:.18rem .52rem; background:#e8f2f0; color:#187a75; font-size:.72rem; }
    .pf-callout { border-left:3px solid #a98b53; background:#f7f4ec; padding:.8rem 1rem; color:#435267; }
    .pf-card { min-height:155px; background:white; border:1px solid #dce3e9; padding:1rem; }
    .pf-card h4 { margin:.15rem 0 .6rem; color:#142a43; font-size:1rem; }
    .pf-card .big { color:#142a43; font-size:1.35rem; font-weight:650; }
    .pf-card .meta { color:#68798a; font-size:.78rem; }
    .pf-evaluation-text { background:#fff; border:1px solid #dce3e9; padding:1.2rem 1.35rem; color:#33465a; font-size:.94rem; line-height:1.9; }
    .pf-page-nav { display:flex; flex-wrap:wrap; gap:.55rem; margin:1rem 0 1.2rem; }
    .pf-page-nav a { color:#19334f; background:#e8edf1; padding:.45rem .72rem; text-decoration:none; font-size:.83rem; }
    div[data-testid="stMetric"] { background:#fff; border:1px solid #dce3e9; padding:.8rem 1rem; }
    div[data-testid="stMetricLabel"] { color:#68798a; }
    div[data-testid="stMetricValue"] { color:#142a43; }
    div.stButton > button[kind="primary"] { background:#142a43; border-color:#142a43; }
    .stDownloadButton > button { background:#142a43; border-color:#142a43; color:#fff; }
    div[data-testid="stDataFrame"] { background:#fff; }
    hr { border-color:#dce3e9; }
    @media print { [data-testid="stSidebar"], [data-testid="stHeader"] { display:none; } }
    </style>
    """,
    unsafe_allow_html=True,
)


def read_subject_spec() -> dict[str, str]:
    """Read the user-supplied project facts; never infer missing values."""
    if not SPEC_PATH.exists():
        return {}
    text = SPEC_PATH.read_text(encoding="utf-8", errors="replace")
    result: dict[str, str] = {}
    for key, pattern in {
        "name": r"사업지\s*[:：]\s*([^\r\n]+)",
        "address": r"위치\s*[:：]\s*([^\r\n]+)",
        "scale": r"규모\s*[:：]\s*([^\r\n]+)",
        "unit_mix": r"평형\s*구성\s*[:：]\s*([^\r\n]+)",
        "move_in": r"입주\s*예정\s*[:：]\s*([^\r\n]+)",
    }.items():
        found = re.search(pattern, text)
        if found:
            result[key] = found.group(1).strip()
    desc = re.search(r"사업지\s*설명\s*[:：]\s*([^\r\n]+)", text)
    if desc:
        result["description"] = re.sub(
            r"총\s*[\d,]+\s*세대\s*규모의\s*", "", desc.group(1).strip()
        )
    for label, val in re.findall(r"^(시도|시군구|법정동)\s*[:：]\s*([^\r\n]+)", text, re.M):
        clean_val = re.sub(r"\s*\(\d+\)\s*$", "", val.strip())
        result[{"시도": "sido", "시군구": "sigungu", "법정동": "dong"}[label]] = clean_val
    code = re.search(r"시군구\s*[:：].*?\((\d{3})\)", text)
    city_code = re.search(r"시도\s*[:：].*?\((\d{2})\)", text)
    if code and city_code:
        result["lawd_cd"] = city_code.group(1) + code.group(1)
    return result


SUBJECT_DEFAULTS = {
    "name": "부천 상동 롯데캐슬",
    "sido": "경기도",
    "sigungu": "부천시 원미구",
    "dong": "상동",
    "lawd_cd": "41192",
}
SPEC = read_subject_spec()


def money_number(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def parse_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def api_key_for_params(raw_key: str) -> str:
    """Normalize either portal key form; requests will URL-encode it once."""
    return unquote(raw_key.strip())


def parse_xml_response(content: bytes) -> tuple[list[dict], int, str, str]:
    root = ET.fromstring(content)
    code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
    message = (
        root.findtext(".//resultMsg")
        or root.findtext(".//errMsg")
        or root.findtext(".//returnAuthMsg")
        or root.findtext(".//returnReasonMsg")
        or ""
    ).strip()
    total = int(root.findtext(".//totalCount") or 0)
    items: list[dict] = []
    for node in root.findall(".//item"):
        items.append({child.tag: (child.text or "").strip() for child in node})
    return items, total, code, message


def request_month(
    service_key: str, lawd_cd: str, year_month: str, page_no: int
) -> tuple[list[dict], int, str, str]:
    params = {
        "serviceKey": api_key_for_params(service_key),
        "LAWD_CD": lawd_cd.strip(),
        "DEAL_YMD": year_month,
        "pageNo": page_no,
        "numOfRows": PAGE_SIZE,
    }
    try:
        response = requests.get(API_URL, params=params, timeout=(6, 18))
    except requests.RequestException as exc:
        return [], 0, "NETWORK_ERROR", str(exc)
    if response.status_code != 200:
        return [], 0, f"HTTP_{response.status_code}", response.reason
    try:
        items, total, code, message = parse_xml_response(response.content)
    except (ET.ParseError, ValueError) as exc:
        return [], 0, "INVALID_XML", str(exc)
    if code and code not in {"000", "00"}:
        if code == "03":
            return [], total, "NO_DATA", message or "조회 결과 없음"
        return [], total, code, message or "API 오류"
    return items, total, code or "000", message or "OK"


def month_keys(start: date, end: date) -> list[str]:
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return keys


def record_date(row: dict) -> date | None:
    try:
        return date(
            int(row.get("dealYear", "0")),
            int(row.get("dealMonth", "0")),
            int(row.get("dealDay", "0")),
        )
    except (TypeError, ValueError):
        return None


def collect_trades(
    service_key: str,
    lawd_cd: str,
    dong: str,
    start: date,
    end: date,
) -> tuple[pd.DataFrame, list[str], int]:
    rows: list[dict] = []
    errors: list[str] = []
    no_data_months = 0
    for month in month_keys(start, end):
        page = 1
        total = 0
        while True:
            items, total, code, message = request_month(service_key, lawd_cd, month, page)
            if code == "NO_DATA":
                no_data_months += 1
                break
            if code not in {"000", "00"}:
                errors.append(f"{month}월 조회 실패 (코드 {code}): {message}")
                break
            rows.extend(items)
            if page * PAGE_SIZE >= total or not items:
                break
            page += 1
            time.sleep(0.04)

    filtered: list[dict] = []
    for row in rows:
        when = record_date(row)
        if when is None or not (start <= when <= end):
            continue
        if dong.strip() and row.get("umdNm", "").strip() != dong.strip():
            continue
        # Cancelled/revoked trades must not enter deal counts or price statistics.
        if row.get("cdealType", "").strip():
            continue
        area = parse_float(row.get("excluUseAr"))
        amount = money_number(row.get("dealAmount"))
        area_py = area / PRICE_PER_PYEONG if area and area > 0 else None
        price_py = amount / area_py if amount is not None and area_py else None
        filtered.append(
            {
                "단지명": row.get("aptNm", "").strip() or "단지명 미기재",
                "법정동": row.get("umdNm", "").strip(),
                "지번": row.get("jibun", "").strip(),
                "계약일": when,
                "전용면적(㎡)": area,
                "거래금액(만원)": amount,
                "평당가격(만원/평)": price_py,
                "층": row.get("floor", "").strip(),
                "건축년도": row.get("buildYear", "").strip(),
                "거래유형": row.get("dealingGbn", "").strip(),
                "출처": "국토교통부 아파트 매매 실거래가 API",
            }
        )
    frame = pd.DataFrame(filtered)
    if not frame.empty:
        frame = frame.sort_values(["계약일", "단지명"], ascending=[False, True]).reset_index(drop=True)
    return frame, errors, no_data_months


def make_complex_summary(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            columns=["단지명", "거래건수", "대표 평당가격(만원/평)", "건축년도", "주요 전용면적(㎡)", "지번"]
        )
    rows = []
    for name, group in trades.groupby("단지명", sort=True):
        prices = [v for v in group["평당가격(만원/평)"].tolist() if pd.notna(v)]
        years = [str(y).strip() for y in group["건축년도"].tolist() if str(y).strip()]
        areas = [v for v in group["전용면적(㎡)"].tolist() if pd.notna(v)]
        lots = [str(v).strip() for v in group["지번"].tolist() if str(v).strip()]
        rows.append(
            {
                "단지명": name,
                "거래건수": len(group),
                "대표 평당가격(만원/평)": median(prices) if prices else None,
                "건축년도": max(set(years), key=years.count) if years else "정보 없음",
                "주요 전용면적(㎡)": round(median(areas), 1) if areas else None,
                "지번": lots[0] if lots else "정보 없음",
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["거래건수", "단지명"], ascending=[False, True]
    ).reset_index(drop=True)


def money_text(value: object, unit: str = "만원") -> str:
    if value is None or pd.isna(value):
        return "정보 없음"
    return f"{float(value):,.0f} {unit}"


def data_table_for_display(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    if "계약일" in output:
        output["계약일"] = output["계약일"].map(lambda d: d.strftime("%Y.%m.%d"))
    if "평당가격(만원/평)" in output:
        output["평당가격(만원/평)"] = output["평당가격(만원/평)"].map(
            lambda v: f"{v:,.0f}" if pd.notna(v) else "정보 없음"
        )
    if "거래금액(만원)" in output:
        output["거래금액(만원)"] = output["거래금액(만원)"].map(
            lambda v: f"{v:,.0f}" if pd.notna(v) else "정보 없음"
        )
    return output


def subject_view() -> dict[str, str]:
    values = {}
    for key in ("name", "sido", "sigungu", "dong", "lawd_cd"):
        values[key] = st.session_state.get(f"subject_{key}", SUBJECT_DEFAULTS[key])
    return values


def set_example_data() -> None:
    for key in ("name", "sido", "sigungu", "dong", "lawd_cd"):
        st.session_state[f"subject_{key}"] = SPEC.get(key, SUBJECT_DEFAULTS[key])
    st.session_state["subject_description"] = SPEC.get("description", "")
    st.session_state["date_range"] = (DEFAULT_START, AS_OF)
    st.session_state["search_start"] = DEFAULT_START
    st.session_state["search_end"] = AS_OF
    st.session_state["example_loaded"] = True


def api_error_message(code: str, message: str) -> str:
    descriptions = {
        "NETWORK_ERROR": "실행 환경에서 API 서버에 연결할 수 없습니다. 네트워크 정책과 인터넷 연결을 확인하세요.",
        "INVALID_XML": "API 응답을 XML로 해석하지 못했습니다.",
        "10": "요청 파라미터 오류입니다. 지역코드(5자리), 계약년월(YYYYMM), 인증키를 확인하세요.",
        "11": "필수 요청 파라미터가 누락되었습니다.",
        "12": "API 주소가 잘못되었거나 서비스가 폐기되었습니다.",
        "20": "활용 신청 승인 상태 또는 인증키 권한을 확인하세요.",
        "22": "일일 호출 허용량을 초과했습니다.",
        "23": "초당 호출 허용량을 초과했습니다. 잠시 후 다시 시도하세요.",
        "30": "등록되지 않은 인증키입니다. Encoding/Decoding 키 형식을 바꾸어 확인하세요.",
        "31": "인증키 사용 기간이 만료되었습니다.",
        "29": "호출 서버 IP가 차단된 상태입니다.",
        "05": "기관 API 연결이 시간 초과되었습니다.",
        "04": "HTTP 호출 또는 API 응답 처리 오류입니다.",
    }
    return descriptions.get(code, message or f"API 오류 ({code})")


def test_api_connection(service_key: str, lawd_cd: str, month: str) -> tuple[bool, str]:
    if not service_key.strip():
        return False, "인증키를 입력해 주세요."
    if not re.fullmatch(r"\d{5}", lawd_cd.strip()):
        return False, "지역코드는 숫자 5자리여야 합니다."
    items, total, code, message = request_month(service_key, lawd_cd.strip(), month, 1)
    if code == "NO_DATA":
        return True, f"API 응답 정상 · {month[:4]}.{month[4:]} 거래자료 없음"
    if code in {"000", "00"}:
        return True, f"API 응답 정상 · {total:,}건 중 {len(items)}건 수신"
    return False, f"{api_error_message(code, message)} (코드 {code})"


def configured_api_key() -> str:
    """Prefer Streamlit Cloud Secrets, then the local .env/process environment."""
    try:
        cloud_key = st.secrets.get("MOLIT_API_KEY", "")
    except FileNotFoundError:
        cloud_key = ""
    return str(cloud_key or os.environ.get("MOLIT_API_KEY", "")).strip()


def build_evaluation(trades: pd.DataFrame, comps: pd.DataFrame, subject: dict) -> str:
    """Create a ten-sentence, fact-led prose draft; every sentence ends with 입니다."""
    if trades.empty:
        return "\n".join([
            "선택한 조사기간과 법정동에서 비교에 사용할 유효 매매 거래가 확인되지 않은 상태입니다.",
            "따라서 인근 단지별 대표 평당가의 범위를 산출할 수 없는 상태입니다.",
            "거래 건수도 비교 단지별로 대조할 자료가 확보되지 않은 상태입니다.",
            "사업지의 목표 분양가 또는 예정 매매가도 사업지 명세에서 확인되지 않은 상태입니다.",
            "이 때문에 사업지 가격이 인근보다 낮은지 판단할 수 없는 상태입니다.",
            "인근 단지의 평당가가 낮을수록 가격 진입 경쟁력의 긍정적 참고가 될 수 있는 요소입니다.",
            "조사기간 중 거래 건수가 많을수록 비교자료의 관측량과 시장 유동성 측면에서 긍정적인 참고가 되는 지표입니다.",
            "실거래가 매매 API는 전용면적과 거래금액 및 계약일을 중심으로 제공하는 자료입니다.",
            "교통과 학군 및 전세가격 등은 확인된 별도 출처가 없어 평가에서 단정할 수 없는 항목입니다.",
            "따라서 본 평가는 자료 확보 후 갱신해야 하는 시장 참고용 초안입니다.",
        ])
    if comps.empty:
        return "\n".join([
            f"선택한 조사기간과 법정동에서 유효 매매 거래 {len(trades)}건이 확인된 상태입니다.",
            "비교 단지가 선택되지 않아 단지별 대표 평당가 범위를 산출할 수 없는 상태입니다.",
            "거래 건수가 많은 단지는 시장 유동성 측면에서 긍정적으로 참고할 수 있는 지표입니다.",
            "현재 선택값으로는 거래량이 가장 많은 단지를 특정할 수 없는 상태입니다.",
            "사업지의 목표 분양가 또는 예정 매매가가 제공되지 않아 직접적인 가격 차이를 판단할 수 없는 상태입니다.",
            "따라서 사업지 가격이 인근보다 낮다고 결론 내릴 수 없는 상태입니다.",
            "단지별 낮은 평당가는 가격 경쟁력 검토에 긍정적인 비교 요소입니다.",
            "단지별 거래건수는 표본의 두께와 시장의 거래 활발성을 보여주는 긍정적인 참고 요소입니다.",
            "면적 구성과 층 및 거래 시점 차이가 가격에 영향을 줄 수 있어 동등 조건 비교가 필요한 상태입니다.",
            "따라서 비교 단지를 선택한 뒤 갱신할 시장 참고용 사업성 평가 초안입니다.",
        ])

    price_values = [
        (str(row["단지명"]), float(row["대표 평당가격(만원/평)"]))
        for _, row in comps.iterrows()
        if pd.notna(row["대표 평당가격(만원/평)"])
    ]
    low = min(price_values, key=lambda item: item[1]) if price_values else None
    high = max(price_values, key=lambda item: item[1]) if price_values else None
    most_traded = comps.sort_values(["거래건수", "단지명"], ascending=[False, True]).iloc[0]
    total_selected_trades = int(comps["거래건수"].sum())
    if low and high and high[1] > 0:
        spread_percent = (high[1] - low[1]) / high[1] * 100
        range_sentence = (
            f"비교 단지의 대표 매매 평당가는 {low[1]:,.0f}~{high[1]:,.0f}만원/평 범위이고 최고가 대비 저가 차이는 {spread_percent:.1f}%입니다."
        )
        low_sentence = (
            f"가장 낮은 평당가를 보인 {low[0]}의 {low[1]:,.0f}만원/평은 비교군 내 상대적인 가격 경쟁력을 보여주는 긍정 요소입니다."
        )
        high_sentence = (
            f"가장 높은 평당가를 보인 {high[0]}의 {high[1]:,.0f}만원/평은 권역 내 상위 가격 수준을 가늠하는 참고 기준입니다."
        )
    else:
        range_sentence = "유효한 평당가를 산출할 수 있는 비교 단지가 없어 가격 범위를 제시하기 어려운 상태입니다."
        low_sentence = "면적과 거래금액이 확인된 단지 자료를 추가 확보하는 것이 가격 비교의 우선 보완사항입니다."
        high_sentence = "확인 가능한 평당가가 없어 상위 가격 수준과의 차이를 계산할 수 없는 상태입니다."
    trade_sentence = (
        f"기간 중 거래가 가장 많았던 {most_traded['단지명']}은 {int(most_traded['거래건수'])}건으로 관측량과 시장 유동성 측면의 긍정 요소입니다."
    )
    if low and low[0] == str(most_traded["단지명"]):
        combined_sentence = f"{low[0]}은 낮은 평당가와 높은 거래건수가 함께 나타나 가격 접근성과 거래 활발성이 겹치는 긍정 사례입니다."
    else:
        combined_sentence = "낮은 평당가와 많은 거래는 가격 접근성과 시장 유동성을 각각 보여주는 긍정적인 비교 요소입니다."
    sentences = [
        f"{subject.get('dong', '대상')} 법정동에서 선택된 {len(comps)}개 단지의 유효 거래 {total_selected_trades}건을 확인한 결과입니다.",
        range_sentence,
        low_sentence,
        high_sentence,
        trade_sentence,
        combined_sentence,
        "거래 건수가 많은 단지는 수요가 관측된 거래 표본이 상대적으로 두터워 시장성 검토에 긍정적인 참고가 되는 지표입니다.",
        "사업지의 목표 분양가가 제공되지 않아 사업지 자체가 인근보다 저렴하다고 결론 내릴 수 없는 상태입니다.",
        "면적 구성과 층 및 거래 시점 차이가 단지별 평당가에 영향을 줄 수 있어 동등 조건의 추가 비교가 필요한 상태입니다.",
        "따라서 낮은 평당가와 높은 거래량은 긍정 요인으로 검토하되 본 의견은 대출 승인이나 금융수익성을 확정하지 않는 사업성 평가 초안입니다.",
    ]
    return "\n".join(sentence if sentence.endswith("입니다.") else sentence for sentence in sentences)


def make_pdf(
    subject: dict,
    trades: pd.DataFrame,
    comps: pd.DataFrame,
    start: date,
    end: date,
    errors: list[str] | None = None,
    retrieved_at: datetime | None = None,
    all_comps: pd.DataFrame | None = None,
) -> bytes:
    if colors is None:
        raise RuntimeError("PDF 라이브러리가 설치되어 있지 않습니다. requirements.txt를 설치하세요.")
    from io import BytesIO

    buffer = BytesIO()
    regular_font = "Helvetica"
    bold_font = "Helvetica-Bold"
    font_candidates = [
        Path(r"C:\Windows\Fonts\malgun.ttf"),
        Path(r"C:\Windows\Fonts\맑은 고딕.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    ]
    for candidate in font_candidates:
        if candidate.exists():
            try:
                pdfmetrics.registerFont(TTFont("PFKorean", str(candidate)))
                regular_font = "PFKorean"
                bold_font = "PFKorean"
                if candidate.name == "NanumGothic.ttf":
                    bold_path = candidate.with_name("NanumGothicBold.ttf")
                    if bold_path.exists():
                        pdfmetrics.registerFont(TTFont("PFKoreanBold", str(bold_path)))
                        bold_font = "PFKoreanBold"
                break
            except Exception:
                continue

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=17 * mm,
        leftMargin=17 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=f"PF Navigator 사업성 평가 - {subject.get('name','사업지')}",
        author="PF Navigator",
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PFTitle", parent=base["Title"], fontName=bold_font, fontSize=18,
        leading=23, textColor=colors.HexColor("#142a43"), alignment=TA_LEFT,
        spaceAfter=5 * mm,
    )
    h_style = ParagraphStyle(
        "PFHeading", parent=base["Heading2"], fontName=bold_font, fontSize=11,
        leading=15, textColor=colors.HexColor("#142a43"), spaceBefore=4 * mm,
        spaceAfter=2 * mm,
    )
    body_style = ParagraphStyle(
        "PFBody", parent=base["BodyText"], fontName=regular_font, fontSize=8.5,
        leading=13, textColor=colors.HexColor("#33465a"), spaceAfter=1.5 * mm,
    )
    small_style = ParagraphStyle(
        "PFSmall", parent=body_style, fontSize=7, leading=10,
        textColor=colors.HexColor("#68798a"),
    )
    table_label_style = ParagraphStyle(
        "PFTableLabel", parent=body_style, fontName=bold_font, fontSize=7.2,
        leading=10, wordWrap="CJK", textColor=colors.HexColor("#526477"),
    )
    table_value_style = ParagraphStyle(
        "PFTableValue", parent=body_style, fontName=regular_font, fontSize=7.2,
        leading=10, wordWrap="CJK", textColor=colors.HexColor("#17283d"),
    )

    story = [
        Paragraph("PF NAVIGATOR　/　사업성 평가 보고서", title_style),
        Paragraph("자료 기준일: 2026.10.21　|　실거래 조사기간: " + start.strftime("%Y.%m.%d") + " ~ " + end.strftime("%Y.%m.%d"), small_style),
        Spacer(1, 4 * mm),
        Paragraph("1. 대상 사업지", h_style),
    ]
    description = SPEC.get("description", "사업지 설명 자료 없음")
    subject_rows = [
        ["사업지명", subject.get("name", "정보 없음"), "소재지", f"{subject.get('sido','')} {subject.get('sigungu','')} {subject.get('dong','')}"],
        ["사업지 주소", SPEC.get("address", "정보 없음"), "계획 규모", SPEC.get("scale", "정보 없음")],
        ["면적 구성", SPEC.get("unit_mix", "정보 없음"), "입주 예정", SPEC.get("move_in", "정보 없음")],
    ]
    subject_paragraph_rows = [
        [Paragraph(escape(str(value)), table_label_style if col in (0, 2) else table_value_style)
         for col, value in enumerate(row)]
        for row in subject_rows
    ]
    stbl = Table(subject_paragraph_rows, colWidths=[23 * mm, 58 * mm, 23 * mm, 69 * mm], repeatRows=0)
    stbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font), ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f5")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2f5")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#526477")),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#dce3e9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([stbl, Spacer(1, 2 * mm), Paragraph(escape(description), body_style)])

    story.append(Paragraph("2. 대상 사업지 및 인근 아파트 비교표", h_style))
    comp_rows = [["구분", "단지명", "준공/입주", "매매 평단가\n(만원/평)", "조사기간 거래", "기준"]]
    comp_rows.append([
        "사업지", subject.get("name", "정보 없음"),
        "입주 예정 " + SPEC.get("move_in", "정보 없음"),
        "거래자료 없음", "정보 없음", subject.get("dong", ""),
    ])
    for _, row in comps.head(10).iterrows():
        comp_rows.append([
            "비교단지", str(row["단지명"]), str(row["건축년도"]),
            f"{row['대표 평당가격(만원/평)']:,.0f}" if pd.notna(row["대표 평당가격(만원/평)"]) else "정보 없음",
            f"{int(row['거래건수'])}건", subject.get("dong", ""),
        ])
    ctbl = Table(comp_rows, colWidths=[17 * mm, 43 * mm, 27 * mm, 30 * mm, 30 * mm, 26 * mm], repeatRows=1)
    ctbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), regular_font), ("FONTSIZE", (0, 0), (-1, -1), 6.7),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#142a43")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8f9")]),
        ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#dce3e9")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([ctbl, Paragraph("가격 집계: 거래별 (거래금액 ÷ (전용면적㎡ ÷ 3.305785))의 단지별 중앙값. 해제 거래는 제외한 기준입니다.", small_style)])
    story.append(Paragraph(
        "선정 기준: 같은 법정동에서 조사기간 중 유효 매매 신고가 확인된 단지. 후보는 거래건수 순으로 제시하고 최종 비교단지는 사용자가 선택합니다. 이는 PF 적합성 점수나 투자 추천이 아닙니다.",
        small_style,
    ))
    if all_comps is not None and not all_comps.empty:
        excluded = all_comps[~all_comps["단지명"].isin(comps["단지명"].tolist())]
        if not excluded.empty:
            excluded_labels = ", ".join(
                f"{row['단지명']}({int(row['거래건수'])}건)" for _, row in excluded.iterrows()
            )
            story.append(Paragraph("미선정 후보 및 사유: " + excluded_labels + " — 사용자 미선택.", small_style))
        else:
            story.append(Paragraph("미선정 후보: 없음.", small_style))
    if errors:
        story.append(Paragraph("조회 상태: 일부 계약월 조회 실패. 실패한 월은 거래자료 없음으로 간주하지 않았습니다.", body_style))
        for error in errors:
            story.append(Paragraph(error, small_style))

    story.append(Paragraph("3. 사업성 평가 의견 초안", h_style))
    evaluation = build_evaluation(trades, comps, subject)
    story.append(Paragraph(escape(evaluation).replace("\n", "<br/>"), body_style))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "자료 출처: 사업지명세서.txt, 국토교통부 아파트 매매 실거래가 API. API 조회일: "
        + (retrieved_at or datetime.now()).strftime("%Y.%m.%d %H:%M")
        + ". 본 문서는 심사 검토용 초안이며 출처가 확인되지 않은 항목은 추정하지 않았습니다.",
        small_style,
    ))

    def page_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#dce3e9"))
        canvas.line(17 * mm, 13 * mm, A4[0] - 17 * mm, 13 * mm)
        canvas.setFont(regular_font, 7)
        canvas.setFillColor(colors.HexColor("#68798a"))
        canvas.drawString(17 * mm, 8 * mm, "PF Navigator · 자료 출처와 기준일은 각 항목 기준")
        canvas.drawRightString(A4[0] - 17 * mm, 8 * mm, f"{document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return buffer.getvalue()


def main() -> None:
    for key in ("name", "sido", "sigungu", "dong", "lawd_cd"):
        st.session_state.setdefault(
            f"subject_{key}", SPEC.get(key, SUBJECT_DEFAULTS[key])
        )
    st.session_state.setdefault("analysis_frame", pd.DataFrame())
    st.session_state.setdefault("analysis_errors", [])
    st.session_state.setdefault("analysis_status", "아직 거래자료를 조회하지 않았습니다.")
    st.session_state.setdefault("selected_comps", [])
    st.session_state.setdefault("date_range", (DEFAULT_START, AS_OF))
    with st.sidebar:
        st.markdown("### PF NAVIGATOR")
        st.caption("아파트 PF 사업성 검토")
        st.divider()
        st.markdown("#### API 연결")
        api_key = configured_api_key()
        if api_key:
            st.success("공공 API 인증키가 설정되어 있습니다.")
        else:
            st.warning("Streamlit Secrets 또는 로컬 .env에 MOLIT_API_KEY를 설정하세요.")
        st.caption("인증키 값은 화면에 표시하지 않습니다.")
        subject = subject_view()
        test_month = (st.session_state.get("date_range", (DEFAULT_START, AS_OF))[1]).strftime("%Y%m")
        if st.button("API 연결 확인", use_container_width=True):
            with st.spinner("국토교통부 API에 연결하고 있습니다…"):
                ok, message = test_api_connection(api_key, subject["lawd_cd"], test_month)
            st.session_state["api_test"] = (ok, message)
        if "api_test" in st.session_state:
            ok, message = st.session_state["api_test"]
            (st.success if ok else st.error)(message)
        st.divider()
        st.caption("자료 기준일 2026.10.21")

    subject = subject_view()
    trades: pd.DataFrame = st.session_state["analysis_frame"]
    comps = make_complex_summary(trades)
    selected_names = st.session_state.get("selected_comps", [])
    selected_comps = comps[comps["단지명"].isin(selected_names)].copy() if not comps.empty else comps
    st.markdown('<div class="pf-kicker">Credit review workspace　/　주거 개발사업</div>', unsafe_allow_html=True)
    st.title("아파트 PF 사업성 평가")
    st.markdown(
        f'<div class="pf-subtitle">{subject.get("name", "사업지")}　·　{subject.get("sido", "")} {subject.get("sigungu", "")} {subject.get("dong", "")}</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<nav class="pf-page-nav"><a href="#section-intro">프로젝트 소개</a><a href="#section-business">사업지</a><a href="#section-apartments">인근 아파트</a><a href="#section-evaluation">사업성 평가</a></nav>', unsafe_allow_html=True)

    st.markdown('<div id="section-intro"></div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-section"><div class="pf-section-title">PF Navigator</div><p>사업지와 동일 법정동의 아파트 매매 실거래를 확인하고, 검증 가능한 시장자료를 바탕으로 심사 검토 초안을 구성합니다.</p><p class="pf-caption">가격·상품성·입지의 사실과 자료 공백을 구분하며, 데이터 출처와 조회일을 보고서에 남깁니다.</p></div>', unsafe_allow_html=True)
    left, mid, right = st.columns(3)
    for col, number, title, copy in [
        (left, "01", "사업지", "대상 사업지의 위치, 규모, 사업 개요와 조사 기준을 확인합니다."),
        (mid, "02", "인근 아파트", "국토교통부 API에서 기간 내 거래를 조회하고 법정동 내 단지를 최대 10개까지 비교합니다."),
        (right, "03", "사업성 평가", "실거래 근거와 한계를 표시한 의견 초안을 만들고 PDF 보고서로 저장합니다."),
    ]:
        with col:
            st.markdown(f'<div class="pf-card"><div class="pf-kicker">{number}</div><h4>{title}</h4><div class="meta">{copy}</div></div>', unsafe_allow_html=True)
    if st.button("예시데이터 불러오기 · 최근 12개월 (2025.10.21~2026.10.21)", type="primary"):
        set_example_data()
        st.rerun()

    st.markdown('<div id="section-business"></div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-section"><div class="pf-section-title"><span class="pf-index">01</span>대상 사업지</div><div class="pf-caption">사업지명세서의 예시 정보를 바탕으로 조회 지역을 확인합니다.</div></div>', unsafe_allow_html=True)
    with st.form("subject_form"):
        c1, c2 = st.columns(2)
        with c1:
            st.text_input("사업지명", key="subject_name")
            st.text_input("시·도", key="subject_sido")
            st.text_input("시·군·구", key="subject_sigungu")
        with c2:
            st.text_input("법정동", key="subject_dong")
            st.text_input("LAWD_CD · 법정동 코드 앞 5자리", key="subject_lawd_cd", max_chars=5)
            st.text_input("사업지 주소", value=SPEC.get("address", ""), disabled=True)
        st.text_area("사업지 설명", value=SPEC.get("description", "사업지 설명 파일에서 확인되지 않았습니다."), height=75, disabled=True)
        st.form_submit_button("사업지 정보 적용", type="primary")
    m1, m2, m3 = st.columns(3)
    m1.metric("계획 규모", SPEC.get("scale", "정보 없음"))
    m2.metric("면적 구성", SPEC.get("unit_mix", "정보 없음"))
    m3.metric("입주 예정", SPEC.get("move_in", "정보 없음"))
    st.caption("출처: 사업지명세서.txt · 외부 검증은 수행하지 않았습니다.")

    st.markdown('<div id="section-apartments"></div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-section"><div class="pf-section-title"><span class="pf-index">02</span>인근 아파트</div><div class="pf-caption">동일 법정동에서 조사기간 내 유효 매매가 확인된 단지를 최대 10개까지 비교합니다.</div></div>', unsafe_allow_html=True)
    with st.form("trade_search_form"):
        d1, d2, d3 = st.columns([1, 1, 1.2])
        saved_range = st.session_state.get("date_range", (DEFAULT_START, AS_OF))
        with d1:
            start_date = st.date_input("시작일", value=saved_range[0], key="search_start")
        with d2:
            end_date = st.date_input("종료일", value=saved_range[1], key="search_end")
        with d3:
            st.text_input("검색 법정동", value=subject.get("dong", ""), disabled=True)
        st.caption(f"API 조건: LAWD_CD {subject.get('lawd_cd')} · 계약년월 월별 조회 · {start_date:%Y.%m.%d} ~ {end_date:%Y.%m.%d}")
        search_clicked = st.form_submit_button("국토교통부 거래 정보 조회", type="primary", use_container_width=True)
    if search_clicked:
        if not api_key.strip():
            st.error("사이드바에 공공데이터포털 인증키를 입력하세요.")
        elif start_date > end_date:
            st.error("시작일은 종료일보다 늦을 수 없습니다.")
        elif not re.fullmatch(r"\d{5}", subject.get("lawd_cd", "")):
            st.error("LAWD_CD는 숫자 5자리여야 합니다.")
        else:
            st.session_state["date_range"] = (start_date, end_date)
            with st.spinner(f"{len(month_keys(start_date, end_date))}개월 단위로 거래자료를 조회하고 있습니다…"):
                found, errors, no_data_months = collect_trades(api_key, subject["lawd_cd"], subject["dong"], start_date, end_date)
            st.session_state["analysis_frame"] = found
            st.session_state["analysis_errors"] = errors
            st.session_state["analysis_start"] = start_date
            st.session_state["analysis_end"] = end_date
            st.session_state["analysis_status"] = (
                "조회 실패" if errors and found.empty else
                "일부 월 조회 실패 · 부분 결과" if errors else
                "정상 조회 · 거래자료 없음" if found.empty else "정상 조회"
            )
            st.session_state["analysis_retrieved_at"] = datetime.now()
            st.session_state["selected_comps"] = make_complex_summary(found)["단지명"].head(10).tolist() if not found.empty else []
            st.rerun()
    errors = st.session_state.get("analysis_errors", [])
    status = st.session_state.get("analysis_status", "")
    if status:
        if status == "정상 조회":
            st.success(f"{status} · 유효 거래 {len(trades):,}건 · 비교 후보 {len(comps)}개")
        elif "부분" in status:
            st.warning(f"{status} · 수집된 거래 {len(trades):,}건. 실패 월은 정보 없음으로 처리하지 않았습니다.")
        elif "자료 없음" in status:
            st.info("정상 조회되었으나 해당 기간·법정동에 유효 거래자료가 없습니다.")
        elif status == "조회 실패":
            st.error("API 조회를 완료하지 못했습니다. 실패는 거래자료 없음과 구분됩니다.")
        for error in errors:
            st.caption(error)
    if not comps.empty:
        max_names = comps["단지명"].tolist()
        defaults = [name for name in st.session_state.get("selected_comps", []) if name in max_names][:10]
        selection = st.multiselect("비교 단지 선택 · 최대 10개", options=max_names, default=defaults, max_selections=10,
            help="동일 법정동 내 신고 거래 단지이며 초기 목록은 거래건수 순입니다.")
        st.session_state["selected_comps"] = selection
        selected_comps = comps[comps["단지명"].isin(selection)].copy()
        st.caption("후보는 기간 내 유효 거래건수 순으로 제시합니다. 거래량 순위는 사업성 평가 점수가 아닙니다.")
        for offset in range(0, len(selected_comps), 3):
            row = selected_comps.iloc[offset:offset + 3]
            cols = st.columns(3)
            for col, (_, item) in zip(cols, row.iterrows()):
                price = money_text(item["대표 평당가격(만원/평)"], "만원/평")
                area = f"전용 {item['주요 전용면적(㎡)']:.1f}㎡" if pd.notna(item["주요 전용면적(㎡)"]) else "면적 정보 없음"
                with col:
                    st.markdown(f'<div class="pf-card"><div class="pf-kicker">동일 법정동 비교 후보</div><h4>{item["단지명"]}</h4><div class="big">{price}</div><div class="meta">기간 내 거래 {int(item["거래건수"])}건 · 건축 {item["건축년도"]}년<br>{area} · 지번 {item["지번"]}</div></div>', unsafe_allow_html=True)
        comparison = pd.DataFrame([{
            "구분": "사업지", "단지명": subject.get("name", "정보 없음"),
            "준공/입주": "입주 예정 " + SPEC.get("move_in", "정보 없음"),
            "매매 평단가(만원/평)": "자료 없음", "조사기간 거래": "자료 없음", "법정동": subject.get("dong", "")
        }] + [{
            "구분": "인근 아파트", "단지명": row["단지명"], "준공/입주": row["건축년도"],
            "매매 평단가(만원/평)": money_text(row["대표 평당가격(만원/평)"], ""),
            "조사기간 거래": f"{int(row['거래건수'])}건", "법정동": subject.get("dong", "")
        } for _, row in selected_comps.iterrows()])
        st.dataframe(comparison, hide_index=True, use_container_width=True)
    elif status and not errors:
        st.info("표시할 비교 후보가 없습니다. 기간과 법정동 코드를 확인하세요.")

    st.markdown('<div id="section-evaluation"></div>', unsafe_allow_html=True)
    st.markdown('<div class="pf-section"><div class="pf-section-title"><span class="pf-index">04</span>사업성 평가</div><div class="pf-caption">가격·거래량 등 관측된 시장자료를 중심으로 작성한 심사 검토 의견 초안입니다.</div></div>', unsafe_allow_html=True)
    evaluation = build_evaluation(trades, selected_comps, subject)
    st.markdown('<div class="pf-evaluation-text">' + escape(evaluation).replace("\n", "<br/>") + '</div>', unsafe_allow_html=True)
    start = st.session_state.get("analysis_start", DEFAULT_START)
    end = st.session_state.get("analysis_end", AS_OF)
    st.caption(f"자료 기준: {subject.get('sido')} {subject.get('sigungu')} {subject.get('dong')} · {start:%Y.%m.%d} ~ {end:%Y.%m.%d}")
    if not trades.empty or status == "정상 조회 · 거래자료 없음":
        try:
            pdf = make_pdf(subject, trades, selected_comps, start, end, errors,
                           st.session_state.get("analysis_retrieved_at"), comps)
            safe_name = re.sub(r"[^가-힣A-Za-z0-9_-]+", "_", subject.get("name", "사업지")).strip("_") or "사업지"
            st.download_button("PDF 보고서 다운로드", data=pdf,
                file_name=f"PF_Navigator_사업성평가_{safe_name}.pdf", mime="application/pdf", type="primary")
        except Exception as exc:
            st.error(f"PDF 생성 실패: {exc}")


if __name__ == "__main__":
    main()

