# -*- coding: utf-8 -*-
import datetime
from typing import Optional, Dict, Any, Union

from core.client import BaseNetSecClient
from .config import (
    URL_GET_ONE_RECORD,
    URL_RUN_RECORDS,
    URL_INDOOR_RECORDS,
    URL_CALENDAR_LIST,
    URL_RUN_DATA_INDEX,
    URL_HISTORY_CONFIG
)
from .models import RunRecordDetail


class HistoryClient(BaseNetSecClient):
    """运动世界校园 7.3.92 历史成绩与回查客户端"""

    def __init__(self,
                 uid: Optional[Union[int, str]] = None,
                 token: Optional[str] = None,
                 unid: Optional[Union[int, str]] = None,
                 device_config: Optional[Dict[str, Any]] = None,
                 proxy: Optional[str] = None):
        super().__init__(uid=uid, token=token, unid=unid, device_config=device_config, proxy=proxy)

    def get_one_record(self, rrid: int) -> Optional[RunRecordDetail]:
        """单次运动结算详情回查 (/api/v70320/runnings/get_one_record)"""
        body = {"rrid": int(rrid)}
        res = self.execute_request("POST", URL_GET_ONE_RECORD, body_plain=body)
        if isinstance(res, dict) and res.get("error") == 10000 and "data" in res and res["data"]:
            return RunRecordDetail.from_api_data(res["data"])
        return None

    def get_detail_map(self, rrids) -> Dict[int, RunRecordDetail]:
        """逐条回查详情，返回 ``{rrid: RunRecordDetail}``。

        存在的理由：列表接口不返回 reasonList，光看列表字段无法知道「为什么未达标」。
        单条失败只跳过该条（状态退回列表字段），不能让一张表整体失败。
        实测 10 条约 4 秒，串行发送 —— 同一个 NetSec 会话并发加密请求容易踩状态。
        """
        out: Dict[int, RunRecordDetail] = {}
        for rrid in rrids:
            if not rrid:
                continue
            try:
                detail = self.get_one_record(rrid=int(rrid))
            except Exception:  # noqa: BLE001 —— 回查是增值信息，失败不该冒泡
                detail = None
            if detail is not None:
                out[int(rrid)] = detail
        return out

    def get_run_records(self, page_num: int = 1, page_size: int = 20, sid: int = 0, sport_type: int = 1) -> dict:
        """分页查询跑步历史列表 (/api/v70230/runnings/records)"""
        body = {
            "pageNum": int(page_num),
            "pageSize": int(page_size),
            "sid": int(sid),
            "type": int(sport_type)
        }
        return self.execute_request("POST", URL_RUN_RECORDS, body_plain=body)

    def get_indoor_records(self, page_num: int = 1, page_size: int = 20, sid: int = 0) -> dict:
        """分页查询室内运动记录 (/api/v74/runnings/indoor/findForPage)"""
        body = {
            "pageNum": int(page_num),
            "pageSize": int(page_size),
            "sid": int(sid)
        }
        return self.execute_request("POST", URL_INDOOR_RECORDS, body_plain=body)

    def get_calendar_list(self, year: Optional[int] = None, month: Optional[int] = None) -> dict:
        """打卡日历列表查询 (/api/v70100/run/calendar/list)"""
        now = datetime.datetime.now()
        y = year or now.year
        m = month or now.month
        body = {"year": int(y), "month": int(m)}
        return self.execute_request("POST", URL_CALENDAR_LIST, body_plain=body)

    def get_history_config(self) -> dict:
        """历史记录配置项查询 (/api/v70100/run/getHistoryConfig)"""
        return self.execute_request("GET", URL_HISTORY_CONFIG)

    def get_data_index(self, sport_type: int = 1) -> dict:
        """数据索引看板查询 (/api/v70100/run/data/index)"""
        return self.execute_request("GET", f"{URL_RUN_DATA_INDEX}?type={sport_type}")


def fetch_detail_map(client, records) -> Dict[int, RunRecordDetail]:
    """给一批列表记录批量回查详情判定，返回 ``{rrid: RunRecordDetail}``。

    调用方既可能是 ``SportClient792``（详情在 ``client.history`` 上），也可能是
    直接构造的 ``HistoryClient``（详情就在自己身上），两种都认。

    任何异常都降级成空字典：状态退回列表字段即可，不能因为回查失败让
    「历史记录」整页报错 —— 那等于用一个附加信息拖垮主功能。
    """
    target = None
    for candidate in (getattr(client, "history", None), client):
        if callable(getattr(candidate, "get_detail_map", None)):
            target = candidate
            break
    if target is None:
        return {}
    rrids = [r.get("rrid") for r in records
             if isinstance(r, dict) and r.get("rrid")]
    if not rrids:
        return {}
    try:
        got = target.get_detail_map(rrids)
    except Exception:  # noqa: BLE001
        return {}
    return got if isinstance(got, dict) else {}
