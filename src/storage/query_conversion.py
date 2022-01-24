from typing import List, Optional, Union

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from storage.schema import AnalysisEntry, FileObjectEntry, FirmwareEntry

FIRMWARE_ORDER = FirmwareEntry.vendor.asc(), FirmwareEntry.device_name.asc()


class QueryConversionException(Exception):
    def get_message(self):
        if self.args:  # pylint: disable=using-constant-test
            return self.args[0]  # pylint: disable=unsubscriptable-object
        return ''


def build_generic_search_query(search_dict: dict, only_fo_parent_firmware: bool, inverted: bool) -> Select:
    if search_dict == {}:
        return select(FirmwareEntry).order_by(*FIRMWARE_ORDER)

    if only_fo_parent_firmware:
        return query_parent_firmware(search_dict, inverted)

    return build_query_from_dict(search_dict).order_by(FileObjectEntry.file_name.asc())


def query_parent_firmware(search_dict: dict, inverted: bool, count: bool = False) -> Select:
    # define alias so that FileObjectEntry can be referenced twice in query
    root_fo = aliased(FileObjectEntry, name='root_fo')
    base_query = (
        select(root_fo.uid)
        # explicitly state FROM because FileObjectEntry is not in select
        .select_from(root_fo, FileObjectEntry)
        # root_fo is in parent_firmware of the FO or FO is the "root file object" of the root_fo
        .filter(FileObjectEntry.root_firmware.any(uid=root_fo.uid) | (FileObjectEntry.uid == root_fo.uid))
    )
    query = build_query_from_dict(search_dict, query=base_query)

    if inverted:
        query_filter = FirmwareEntry.uid.notin_(query)
    else:
        query_filter = FirmwareEntry.uid.in_(query)

    if count:
        return select(func.count(FirmwareEntry.uid)).filter(query_filter)
    return select(FirmwareEntry).filter(query_filter).order_by(*FIRMWARE_ORDER)


def build_query_from_dict(query_dict: dict, query: Optional[Select] = None) -> Select:  # pylint: disable=too-complex
    '''
    Builds an ``sqlalchemy.orm.Query`` object from a query in dict form.
    '''
    if query is None:
        query = select(FileObjectEntry)

    if '_id' in query_dict:
        # FixMe?: backwards compatible for binary search
        query_dict['uid'] = query_dict.pop('_id')

    analysis_keys = [key for key in query_dict if key.startswith('processed_analysis')]
    if analysis_keys:
        query = _add_analysis_filter_to_query(analysis_keys, query, query_dict)

    firmware_keys = [key for key in query_dict if not key == 'uid' and hasattr(FirmwareEntry, key)]
    if firmware_keys:
        query = query.join(FirmwareEntry, FirmwareEntry.uid == FileObjectEntry.uid)
        query = _add_filters_for_attribute_list(firmware_keys, FirmwareEntry, query, query_dict)

    file_object_keys = [key for key in query_dict if hasattr(FileObjectEntry, key)]
    if file_object_keys:
        query = _add_filters_for_attribute_list(file_object_keys, FileObjectEntry, query, query_dict)

    return query


def _add_filters_for_attribute_list(attribute_list: List[str], table, query: Select, query_dict: dict) -> Select:
    for key in attribute_list:
        column = _get_column(key, table)
        query = _apply_filter(query, column, query_dict, key)
    return query


def _apply_filter(query: Select, column, query_dict: dict, key: str) -> Select:
    if not isinstance(query_dict[key], dict):
        query = query.filter(column == query_dict[key])
    elif '$regex' in query_dict[key]:
        query = query.filter(column.op('~')(query_dict[key]['$regex']))
    elif '$in' in query_dict[key]:  # filter by list
        query = query.filter(column.in_(query_dict[key]['$in']))
    elif '$lt' in query_dict[key]:  # less than
        query = query.filter(column < query_dict[key]['$lt'])
    elif '$gt' in query_dict[key]:  # greater than
        query = query.filter(column > query_dict[key]['$gt'])
    elif '$contains' in query_dict[key]:  # array contains value
        query = query.filter(column.contains(query_dict[key]['$contains']))
    else:
        raise QueryConversionException(f'Search options currently unsupported: {query_dict[key]}')
    return query


def _get_column(key: str, table: Union[FirmwareEntry, FileObjectEntry, AnalysisEntry]):
    column = getattr(table, key)
    if key == 'release_date':  # special case: Date column -> convert to string
        return func.to_char(column, 'YYYY-MM-DD')
    return column


def _add_analysis_filter_to_query(analysis_keys: List[str], query: Select, query_dict: dict) -> Select:
    query = query.join(AnalysisEntry, AnalysisEntry.uid == FileObjectEntry.uid)
    for key in analysis_keys:  # type: str
        _, plugin, json_key = key.split('.', maxsplit=2)
        if hasattr(AnalysisEntry, key):
            if json_key == 'summary':  # special case: array field -> contains()
                needle = query_dict[key] if isinstance(query_dict[key], list) else [query_dict[key]]
                query = query.filter(AnalysisEntry.summary.contains(needle), AnalysisEntry.plugin == plugin)
            else:
                query = query.filter(getattr(AnalysisEntry, key) == query_dict[key])
        else:  # no meta field, actual analysis result key
            query = query.filter(AnalysisEntry.plugin == plugin)
            column = AnalysisEntry.result
            for nested_key in json_key.split('.'):
                column = column[nested_key]
            query = _apply_filter(query, column.astext, query_dict, key)
    return query
