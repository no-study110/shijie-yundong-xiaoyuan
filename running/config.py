# -*- coding: utf-8 -*-
from core.config import HOST_RUN, SIGN_SALT

# 7.3.92 跑步提交与 OBS 华为云网络端点
URL_OBS_TEMPORARY_URL = f"{HOST_RUN}/api/obs/temporary/url"
URL_SAVE_RECORD = f"{HOST_RUN}/api/v70260/runnings/save/record"
URL_RUN_DETAIL_CONFIG = f"{HOST_RUN}/api/v70340/activity/rundetail/config"

# 7.3.92 实测逐字节一致的 33 字节静态加盐（统一收敛在 core.config.SIGN_SALT）
SPORT_STATIC_SALT = SIGN_SALT
OBS_BUCKET_NAME = "iydsj-hbase-hot"
