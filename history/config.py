# -*- coding: utf-8 -*-
from core.config import HOST_RUN, HOST_DISCOVERY

URL_GET_ONE_RECORD = f"{HOST_RUN}/api/v70320/runnings/get_one_record"
URL_RUN_RECORDS = f"{HOST_RUN}/api/v70230/runnings/records"
URL_INDOOR_RECORDS = f"{HOST_RUN}/api/v74/runnings/indoor/findForPage"
URL_CALENDAR_LIST = f"{HOST_DISCOVERY}/api/v70100/run/calendar/list"
URL_RUN_DATA_INDEX = f"{HOST_RUN}/api/v70100/run/data/index"
URL_HISTORY_CONFIG = f"{HOST_RUN}/api/v70100/run/getHistoryConfig"
