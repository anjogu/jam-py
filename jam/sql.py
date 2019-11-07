import sys
import zlib
import base64
import json
import pickle
import datetime

from .common import consts, error_message, json_defaul_handler
from .execute import apply_sql
import jam.db.db_modules as db_modules
from werkzeug._compat import iteritems, text_type, integer_types, string_types, to_bytes, to_unicode

class SQL(object):

    def get_next_id(self, db_module=None):
        if db_module is None:
            db_module = self.task.db_module
        sql = db_module.next_sequence_value_sql(self.gen_name)
        if sql:
            rec = self.task.select(sql)
            if rec:
                if rec[0][0]:
                    return int(rec[0][0])

    def __execute(self, cursor, sql, params=None):
        try:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
        except Exception as x:
            self.log.exception('Error: %s\n query: %s\n params: %s' % (error_message(x), sql, params))
            raise

    def __insert_record(self, cursor, db_module, changes, details_changes):
        if self._deleted_flag:
            self._deleted_flag_field.data = 0
        pk = None
        if self._primary_key:
            pk = self._primary_key_field
        auto_pk = not db_module.get_lastrowid is None
        after_sql = None
        if pk :
            if auto_pk:
                if pk.data and hasattr(db_module, 'set_identity_insert'):
                    sql = db_module.set_identity_insert(self.table_name, True)
                    self.__execute(cursor, sql)
                    after_sql = db_module.set_identity_insert(self.table_name, False)
            else:
                sql = db_module.next_sequence_value_sql(self.gen_name)
                self.__execute(cursor, sql)
                r = cursor.fetchone()
                pk.data = r[0]
        row = []
        fields = []
        values = []
        index = 0
        for field in self.fields:
            if not (field == pk and auto_pk and not pk.data):
                index += 1
                fields.append('"%s"' % field.db_field_name)
                values.append('%s' % db_module.value_literal(index))
                value = (field.data, field.data_type)
                row.append(value)
        fields = ', '.join(fields)
        values = ', '.join(values)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % \
            (self.table_name, fields, values)
        row = db_module.process_sql_params(row, cursor)
        self.__execute(cursor, sql, row)
        if after_sql:
            self.__execute(cursor, after_sql)
        if db_module.get_lastrowid and not pk.data:
            pk.data = db_module.get_lastrowid(cursor)
        changes.append([self.get_rec_info()[consts.REC_LOG_REC], self._dataset[self.rec_no], details_changes])

    def __update_record(self, cursor, db_module, changes, details_changes):
        row = []
        fields = []
        index = 0
        pk = self._primary_key_field
        command = 'UPDATE "%s" SET ' % self.table_name
        for field in self.fields:
            if field.field_name != self._record_version and field != pk:
                index += 1
                fields.append('"%s"=%s' % (field.db_field_name, db_module.value_literal(index)))
                value = (field.data, field.data_type)
                if field.field_name == self._deleted_flag:
                    value = (0, field.data_type)
                row.append(value)
        fields = ', '.join(fields)
        if self.edit_lock and self._record_version:
            fields = ' %s, "%s"=COALESCE("%s", 0)+1' % \
            (fields, self._record_version_db_field_name, self._record_version_db_field_name)
        if self._primary_key_field.data_type == consts.TEXT:
            id_literal = "'%s'" % self._primary_key_field.value
        else:
            id_literal = "%s" % self._primary_key_field.value
        where = ' WHERE "%s" = %s' % (self._primary_key_db_field_name, id_literal)
        sql = ''.join([command, fields, where])
        row = db_module.process_sql_params(row, cursor)
        self.__execute(cursor, sql, row)
        if self.edit_lock and self._record_version:
            self.__execute(cursor, 'SELECT "%s" FROM "%s" WHERE "%s"=%s' % \
                (self._record_version_db_field_name, self.table_name, \
                self._primary_key_db_field_name, pk.data))
            r = cursor.fetchone()
            record_version = r[0]
            if record_version != self._record_version_field.value + 1:
                raise Exception(consts.language('edit_record_modified'))
            self._record_version_field.data = record_version
        changes.append([self.get_rec_info()[consts.REC_LOG_REC], self._dataset[self.rec_no], details_changes])

    def __delete_record(self, cursor, db_module, changes, details_changes):
        log_rec = self.get_rec_info()[consts.REC_LOG_REC]
        soft_delete = self.soft_delete
        if self.master:
            soft_delete = self.master.soft_delete
        if self._primary_key_field.data_type == consts.TEXT:
            id_literal = "'%s'" % self._primary_key_field.value
        else:
            id_literal = "%s" % self._primary_key_field.value
        if soft_delete:
            sql = 'UPDATE "%s" SET "%s" = 1 WHERE "%s" = %s' % \
                (self.table_name, self._deleted_flag_db_field_name,
                self._primary_key_db_field_name, id_literal)
        else:
            sql = 'DELETE FROM "%s" WHERE "%s" = %s' % \
                (self.table_name, self._primary_key_db_field_name, id_literal)
        self.__execute(cursor, sql)
        changes.append([log_rec, None, None])

    def __get_user(self):
        user = None
        if self.session:
            try:
                user = self.session.get('user_info')['user_name']
            except:
                pass
        return user

    def __save_history(self, connection, cursor, db_module):
        if self.task.history_item and self.keep_history and self.record_status != consts.RECORD_DETAILS_MODIFIED:
            changes = None
            user = self.__get_user()
            item_id = self.ID
            if self.master:
                item_id = self.prototype.ID
            if self.record_status != consts.RECORD_DELETED:
                old_rec = self.get_rec_info()[consts.REC_OLD_REC]
                new_rec = self._dataset[self.rec_no]
                f_list = []
                for f in self.fields:
                    if not f.system_field():
                        new = new_rec[f.bind_index]
                        old = None
                        if old_rec:
                            old = old_rec[f.bind_index]
                        if old != new:
                            f_list.append([f.ID, new])
                changes_str = json.dumps(f_list, separators=(',',':'), default=json_defaul_handler)
                changes = ('%s%s' % ('0', changes_str), consts.LONGTEXT)
            params = [item_id, self._primary_key_field.value, self.record_status, changes, user, datetime.datetime.now()]
            params = db_module.process_sql_params(params, cursor)
            self.__execute(cursor, self.task.history_sql, params)

    def __update_deleted_detail(self, detail, cursor, db_module):
        fields = [detail._primary_key]
        detail.open(fields=fields, open_empty=True)
        sql = 'SELECT "%s" FROM "%s" WHERE "%s" = %s AND "%s" = %s and "%s" = 0' % \
            (detail._primary_key_db_field_name, detail.table_name,
            detail._master_id_db_field_name, self.ID,
            detail._master_rec_id_db_field_name, self._primary_key_field.value,
            detail._deleted_flag_db_field_name)
        try:
            cursor.execute(sql)
            rows = db_module.process_sql_result(cursor.fetchall())
        except Exception as x:
            self.log.exception(error_message(x))
            raise Exception(x)
        detail._dataset = rows

    def __delete_detail_records(self, connection, cursor, detail, db_module):
        if self._primary_key_field.data_type == consts.TEXT:
            id_literal = "'%s'" % self._primary_key_field.value
        else:
            id_literal = "%s" % self._primary_key_field.value
        if detail._master_id:
            if self.soft_delete:
                sql = 'UPDATE "%s" SET "%s" = 1 WHERE "%s" = %s AND "%s" = %s' % \
                    (detail.table_name, detail._deleted_flag_db_field_name, detail._master_id_db_field_name, \
                    self.ID, detail._master_rec_id_db_field_name, id_literal)
            else:
                sql = 'DELETE FROM "%s" WHERE "%s" = %s AND "%s" = %s' % \
                    (detail.table_name, detail._master_id_db_field_name, self.ID, \
                    detail._master_rec_id_db_field_name, id_literal)
        else:
            if self.soft_delete:
                sql = 'UPDATE "%s" SET "%s" = 1 WHERE "%s" = %s' % \
                    (detail.table_name, detail._deleted_flag_db_field_name, \
                    detail._master_rec_id_db_field_name, id_literal)
            else:
                sql = 'DELETE FROM "%s" WHERE "%s" = %s' % \
                    (detail.table_name, detail._master_rec_id_db_field_name, id_literal)
        if len(detail.details) or detail.keep_history:
            self.__update_deleted_detail(detail, cursor, db_module)
            if detail.keep_history:
                for d in detail:
                    params = [detail.prototype.ID, d._primary_key_field.data,
                        consts.RECORD_DELETED, None, self.__get_user(), datetime.datetime.now()]
                    self.__execute(cursor, self.task.history_sql, db_module.process_sql_params(params, cursor))
            if len(detail.details):
                for it in detail:
                    for d in detail.details:
                        detail.__delete_detail_records(connection, cursor, d, db_module)
        self.__execute(cursor, sql)

    def __process_record(self, connection, cursor, safe, db_module, changes, details_changes):
        if self.master:
            if self._master_id:
                self._master_id_field.data = self.master.ID
            self._master_rec_id_field.data = self.master._primary_key_field.value
        if self.record_status == consts.RECORD_INSERTED:
            if safe and not self.can_create():
                raise Exception(consts.language('cant_create') % self.item_caption)
            self.__insert_record(cursor, db_module, changes, details_changes)
        elif self.record_status == consts.RECORD_MODIFIED:
            if safe and not self.can_edit():
                raise Exception(consts.language('cant_edit') % self.item_caption)
            self.__update_record(cursor, db_module, changes, details_changes)
        elif self.record_status == consts.RECORD_DETAILS_MODIFIED:
            pass
        elif self.record_status == consts.RECORD_DELETED:
            if safe and not self.can_delete():
                raise Exception(consts.language('cant_delete') % self.item_caption)
            self.__delete_record(cursor, db_module, changes, details_changes)
        else:
            raise Exception('execute_delta - invalid %s record_status %s, record: %s' % \
                (self.item_name, self.record_status, self._dataset[self.rec_no]))
        self.__save_history(connection, cursor, db_module)

    def __process_records(self, connection, cursor, safe, db_module, changes):
        for it in self:
            details = []
            it.__process_record(connection, cursor, safe, db_module, changes, details)
            for detail in self.details:
                detail_changes = []
                detail_result = {'ID': str(detail.ID), 'changes': detail_changes}
                details.append(detail_result)
                if self.record_status == consts.RECORD_DELETED:
                    self.__delete_detail_records(connection, cursor, detail, db_module)
                else:
                    detail.__process_records(connection, cursor, safe, db_module, detail_changes)

    def process_changes(self, connection, params=None, db_module=None):
        error = None
        safe = False
        if params:
            safe = params['__safe']
        if db_module is None:
            db_module = self.task.db_module
        changes = []
        result = {'ID': str(self.ID), 'changes': changes}
        cursor = connection.cursor()
        self.__process_records(connection, cursor, safe, db_module, changes)
        return result, error

    def apply_sql(self, params=None, db_module=None):
        return apply_sql(self, params=None, db_module=None)

    def table_alias(self):
        return '"%s"' % self.table_name

    def lookup_table_alias(self, field):
        if field.master_field:
            return '%s_%d' % (field.lookup_item.table_name, field.master_field.ID)
        else:
            return '%s_%d' % (field.lookup_item.table_name, field.ID)

    def lookup_table_alias1(self, field):
        return self.lookup_table_alias(field) + '_' + field.lookup_db_field

    def lookup_table_alias2(self, field):
        return self.lookup_table_alias1(field) + '_' + field.lookup_db_field1

    def field_alias(self, field, db_module):
        return '%s_%s' % (field.db_field_name, db_module.identifier_case('LOOKUP'))

    def lookup_field_sql(self, field, db_module):
        if field.lookup_item:
            if field.lookup_field2:
                field_sql = '%s."%s"' % (self.lookup_table_alias2(field), field.lookup_db_field2)
            elif field.lookup_field1:
                field_sql = '%s."%s"' % (self.lookup_table_alias1(field), field.lookup_db_field1)
            else:
                if field.data_type == consts.KEYS:
                    field_sql = 'NULL'
                else:
                    field_sql = '%s."%s"' % (self.lookup_table_alias(field), field.lookup_db_field)
            return field_sql

    def fields_clause(self, query, fields, db_module=None):
        summary = query.get('__summary')
        if db_module is None:
            db_module = self.task.db_module
        funcs = query.get('__funcs')
        if funcs:
            functions = {}
            for key, value in iteritems(funcs):
                functions[key.upper()] = value
        sql = []
        for i, field in enumerate(fields):
            if i == 0 and summary:
                sql.append(db_module.identifier_case('count(*)'))
            elif field.master_field:
                pass
            else:
                field_sql = '%s."%s"' % (self.table_alias(), field.db_field_name)
                func = None
                if funcs:
                    func = functions.get(field.field_name.upper())
                if func:
                    field_sql = '%s(%s) %s "%s"' % (func.upper(), field_sql, db_module.FIELD_AS, field.db_field_name)
                sql.append(field_sql)
        if query['__expanded']:
            for i, field in enumerate(fields):
                if i == 0 and summary:
                    continue
                field_sql = self.lookup_field_sql(field, db_module)
                field_alias = self.field_alias(field, db_module)
                if field_sql:
                    if funcs:
                        func = functions.get(field.field_name.upper())
                    if func:
                        field_sql = '%s(%s) %s "%s"' % (func.upper(), field_sql, db_module.FIELD_AS, field_alias)
                    else:
                        field_sql = '%s %s %s' % (field_sql, db_module.FIELD_AS, field_alias)
                    sql.append(field_sql)
        sql = ', '.join(sql)
        return sql

    def from_clause(self, query, fields, db_module=None):
        if db_module is None:
            db_module = self.task.db_module
        result = []
        result.append(db_module.FROM % (self.table_name, self.table_alias()))
        if query['__expanded']:
            joins = {}
            for field in fields:
                if field.lookup_item and field.data_type != consts.KEYS:
                    alias = self.lookup_table_alias(field)
                    cur_field = field
                    if field.master_field:
                        cur_field = field.master_field
                    if not joins.get(alias):
                        primary_key_field_name = field.lookup_item._primary_key_db_field_name
                        result.append('%s ON %s."%s" = %s."%s"' % (
                            db_module.LEFT_OUTER_JOIN % (field.lookup_item.table_name, self.lookup_table_alias(field)),
                            self.table_alias(),
                            cur_field.db_field_name,
                            self.lookup_table_alias(field),
                            primary_key_field_name
                        ))
                        joins[alias] = True
                if field.lookup_item1:
                    alias = self.lookup_table_alias1(field)
                    if not joins.get(alias):
                        primary_key_field_name = field.lookup_item1._primary_key_db_field_name
                        result.append('%s ON %s."%s" = %s."%s"' % (
                            db_module.LEFT_OUTER_JOIN % (field.lookup_item1.table_name, self.lookup_table_alias1(field)),
                            self.lookup_table_alias(field),
                            field.lookup_db_field,
                            self.lookup_table_alias1(field),
                            primary_key_field_name
                        ))
                        joins[alias] = True
                if field.lookup_item2:
                    alias = self.lookup_table_alias2(field)
                    if not joins.get(alias):
                        primary_key_field_name = field.lookup_item2._primary_key_db_field_name
                        result.append('%s ON %s."%s" = %s."%s"' % (
                            db_module.LEFT_OUTER_JOIN % (field.lookup_item2.table_name, self.lookup_table_alias2(field)),
                            self.lookup_table_alias1(field),
                            field.lookup_db_field1,
                            self.lookup_table_alias2(field),
                            primary_key_field_name
                        ))
                        joins[alias] = True
        return ' '.join(result)

    def _get_filter_sign(self, filter_type, value, db_module):
        result = consts.FILTER_SIGN[filter_type]
        if filter_type == consts.FILTER_ISNULL:
            if value:
                result = 'IS NULL'
            else:
                result = 'IS NOT NULL'
        if (result == 'LIKE'):
            result = db_module.LIKE
        return result

    def _convert_field_value(self, field, value, filter_type, db_module):
        data_type = field.data_type
        if filter_type and filter_type in [consts.FILTER_CONTAINS, consts.FILTER_STARTWITH, consts.FILTER_ENDWITH]:
            if data_type == consts.FLOAT:
                value = field.str_to_float(value)
            elif data_type == consts.CURRENCY:
                value = field.str_to_cur(value)
            if type(value) == float:
                if int(value) == value:
                    value = str(int(value)) + '.'
                else:
                    value = str(value)
            return value
        else:
            if data_type == consts.DATE:
                if type(value) in string_types:
                    result = value
                else:
                    result = value.strftime('%Y-%m-%d')
                return db_module.cast_date(result)
            elif data_type == consts.DATETIME:
                if type(value) in string_types:
                    result = value
                else:
                    result = value.strftime('%Y-%m-%d %H:%M')
                result = db_module.cast_datetime(result)
                return result
            elif data_type == consts.INTEGER:
                if type(value) in integer_types or type(value) in string_types and value.isdigit():
                    return str(value)
                else:
                    return "'" + value + "'"
            elif data_type == consts.BOOLEAN:
                if value:
                    return '1'
                else:
                    return '0'
            elif data_type == consts.TEXT:
                return "'" + to_unicode(value) + "'"
            elif data_type in (consts.FLOAT, consts.CURRENCY):
                return str(float(value))
            else:
                return value

    def _escape_search(self, value, esc_char):
        result = ''
        found = False
        for ch in value:
            if ch == "'":
                ch = ch + ch
            elif ch in ['_', '%']:
                ch = esc_char + ch
                found = True
            result += ch
        return result, found

    def _get_condition(self, field, filter_type, value, db_module):
        esc_char = '/'
        cond_field_name = '%s."%s"' % (self.table_alias(), field.db_field_name)
        if type(value) == str:
            value = to_unicode(value, 'utf-8')
        filter_sign = self._get_filter_sign(filter_type, value, db_module)
        cond_string = '%s %s %s'
        if filter_type in (consts.FILTER_IN, consts.FILTER_NOT_IN):
            values = [self._convert_field_value(field, v, filter_type, db_module) for v in value if v is not None]
            value = '(%s)' % ', '.join(values)
        elif filter_type == consts.FILTER_RANGE:
            value = self._convert_field_value(field, value[0], filter_type, db_module) + \
                ' AND ' + self._convert_field_value(field, value[1], filter_type, db_module)
        elif filter_type == consts.FILTER_ISNULL:
            value = ''
        else:
            value = self._convert_field_value(field, value, filter_type, db_module)
            if filter_type in [consts.FILTER_CONTAINS, consts.FILTER_STARTWITH, consts.FILTER_ENDWITH]:
                value, esc_found = self._escape_search(value, esc_char)
                if field.lookup_item:
                    if field.lookup_item1:
                        cond_field_name = '%s."%s"' % (self.lookup_table_alias1(field), field.lookup_db_field1)
                    else:
                        if field.data_type == consts.KEYS:
                            cond_field_name = '%s."%s"' % (self.table_alias(), field.db_field_name)
                        else:
                            cond_field_name = '%s."%s"' % (self.lookup_table_alias(field), field.lookup_db_field)

                if filter_type == consts.FILTER_CONTAINS:
                    value = '%' + value + '%'
                elif filter_type == consts.FILTER_STARTWITH:
                    value = value + '%'
                elif filter_type == consts.FILTER_ENDWITH:
                    value = '%' + value
                cond_field_name, value = db_module.convert_like(cond_field_name, value, field.data_type)
                if esc_found:
                    value = "'" + value + "' ESCAPE '" + esc_char + "'"
                else:
                    value = "'" + value + "'"
        sql = cond_string % (cond_field_name, filter_sign, value)
        if field.data_type == consts.BOOLEAN and value == '0':
            if filter_sign == '=':
                sql = '(' + sql + ' OR %s IS NULL)' % cond_field_name
            elif filter_sign == '<>':
                sql = '(' + sql + ' AND %s IS NOT NULL)' % cond_field_name
            else:
                raise Exception('sql.py where_clause method: boolen field condition may give ambiguious results.')
        return sql

    def add_master_conditions(self, query, conditions):
        master_id = query['__master_id']
        master_rec_id = query['__master_rec_id']
        if master_id and master_rec_id:
            if self._master_id:
                conditions.append('%s."%s"=%s' % \
                    (self.table_alias(), self._master_id_db_field_name, str(master_id)))
                conditions.append('%s."%s"=%s' % \
                    (self.table_alias(), self._master_rec_id_db_field_name, str(master_rec_id)))

    def where_clause(self, query, db_module=None):
        if db_module is None:
            db_module = self.task.db_module
        conditions = []
        if self.master:
            self.add_master_conditions(query, conditions)
        filters = query['__filters']
        deleted_in_filters = False
        if filters:
            for field_name, filter_type, value in filters:
                if not value is None:
                    field = self._field_by_name(field_name)
                    if field_name == self._deleted_flag:
                        deleted_in_filters = True
                    if filter_type == consts.FILTER_CONTAINS_ALL:
                        values = value.split()
                        for val in values:
                            conditions.append(self._get_condition(field, consts.FILTER_CONTAINS, val, db_module))
                    elif filter_type in [consts.FILTER_IN, consts.FILTER_NOT_IN] and \
                        type(value) in [tuple, list] and len(value) == 0:
                        conditions.append('%s."%s" IN (NULL)' % (self.table_alias(), self._primary_key_db_field_name))
                    else:
                        conditions.append(self._get_condition(field, filter_type, value, db_module))
        if not deleted_in_filters and self._deleted_flag:
            conditions.append('%s."%s"=0' % (self.table_alias(), self._deleted_flag_db_field_name))
        result = ' AND '.join(conditions)
        if result:
            result = ' WHERE ' + result
        return result

    def group_clause(self, query, fields, db_module=None):
        if db_module is None:
            db_module = self.task.db_module
        group_fields = query.get('__group_by')
        funcs = query.get('__funcs')
        if funcs:
            functions = {}
            for key, value in iteritems(funcs):
                functions[key.upper()] = value
        result = ''
        if group_fields:
            for field_name in group_fields:
                field = self._field_by_name(field_name)
                if query['__expanded'] and field.lookup_item and field.data_type != consts.KEYS:
                    func = functions.get(field.field_name.upper())
                    if func:
                        result += '%s."%s", ' % (self.table_alias(), field.db_field_name)
                    else:
                        result += '%s, %s."%s", ' % (self.lookup_field_sql(field, db_module),
                            self.table_alias(), field.db_field_name)
                else:
                    result += '%s."%s", ' % (self.table_alias(), field.db_field_name)
            if result:
                result = result[:-2]
                result = ' GROUP BY ' + result
            return result
        else:
            return ''

    def order_clause(self, query, db_module=None):
        limit = query.get('__limit')
        if limit and not query.get('__order') and self._primary_key:
            query['__order'] = [[self._primary_key, False]]
        if query.get('__funcs') and not query.get('__group_by'):
            return ''
        funcs = query.get('__funcs')
        functions = {}
        if funcs:
            for key, value in iteritems(funcs):
                functions[key.upper()] = value
        if db_module is None:
            db_module = self.task.db_module
        order_list = query.get('__order', [])
        orders = []
        for order in order_list:
            field = self._field_by_name(order[0])
            if field:
                func = functions.get(field.field_name.upper())
                if not query['__expanded'] and field.lookup_item1:
                   orders = []
                   break
                if query['__expanded'] and field.lookup_item:
                    if field.data_type == consts.KEYS:
                        ord_str = '%s."%s"' % (self.table_alias(), field.db_field_name)
                    else:
                        if func:
                            ord_str = self.field_alias(field, db_module)
                        else:
                            ord_str = self.lookup_field_sql(field, db_module)
                else:
                    if func:
                        if db_module.DATABASE == 'MSSQL' and limit:
                            ord_str = '%s(%s."%s")' %  (func, self.table_alias(), field.db_field_name)
                        else:
                            ord_str = '"%s"' % field.db_field_name
                    else:
                        ord_str = '%s."%s"' % (self.table_alias(), field.db_field_name)
                if order[1]:
                    if hasattr(db_module, 'DESC'):
                        ord_str += ' ' + db_module.DESC
                    else:
                        ord_str += ' DESC'
                orders.append(ord_str)
        if orders:
             result = ' ORDER BY %s' % ', '.join(orders)
        else:
            result = ''
        return result

    def split_query(self, query):
        MAX_IN_LIST = 1000
        filters = query['__filters']
        filter_index = -1
        max_list = 0
        if filters:
            for i, f in enumerate(filters):
                field_name, filter_type, value = f
                if filter_type in [consts.FILTER_IN, consts.FILTER_NOT_IN]:
                    length = len(value)
                    if length > MAX_IN_LIST and length > max_list:
                        max_list = length
                        filter_index = i
        if filter_index != -1:
            lists = []
            value_list = filters[filter_index][2]
            while True:
                values = value_list[0:MAX_IN_LIST]
                if values:
                    lists.append(values)
                value_list = value_list[MAX_IN_LIST:]
                if not value_list:
                    break;
            return filter_index, lists

    def get_select_queries(self, query, db_module=None):
        result = []
        filter_in_info = self.split_query(query)
        if filter_in_info:
            filter_index, lists = filter_in_info
            for lst in lists:
                query['__limit'] = None
                query['__offset'] = None
                query['__filters'][filter_index][2] = lst
                result.append(self.get_select_query(query, db_module))
        else:
            result.append(self.get_select_query(query, db_module))
        return result

    def get_select_statement(self, query, db_module=None): # depricated
        return self.get_select_query(query, db_module)

    def get_select_query(self, query, db_module=None):
        try:
            if db_module is None:
                db_module = self.task.db_module
            field_list = query['__fields']
            if len(field_list):
                fields = [self._field_by_name(field_name) for field_name in field_list]
            else:
                fields = self._fields
            fields_clause = self.fields_clause(query, fields, db_module)
            from_clause = self.from_clause(query, fields, db_module)
            where_clause = self.where_clause(query, db_module)
            group_clause = self.group_clause(query, fields, db_module)
            order_clause = self.order_clause(query, db_module)
            sql = db_module.get_select(query, fields_clause, from_clause, where_clause, group_clause, order_clause, fields)
            return sql
        except Exception as e:
            self.log.exception(error_message(e))
            raise

    def get_record_count_queries(self, query, db_module=None):
        result = []
        filter_in_info = self.split_query(query)
        if filter_in_info:
            filter_index, lists = filter_in_info
            for lst in lists:
                query['__filters'][filter_index][2] = lst
                result.append(self.get_record_count_query(query, db_module))
        else:
            result.append(self.get_record_count_query(query, db_module))
        return result

    def get_record_count_query(self, query, db_module=None):
        if db_module is None:
            db_module = self.task.db_module
        fields = []
        filters = query['__filters']
        if filters:
            for (field_name, filter_type, value) in filters:
                fields.append(self._field_by_name(field_name))
        sql = 'SELECT COUNT(*) FROM %s %s' % (self.from_clause(query, fields, db_module),
            self.where_clause(query, db_module))
        return sql

    def empty_table_sql(self):
        return 'DELETE FROM %s' % self.table_name

    def create_table_sql(self, db_type, table_name, fields, gen_name=None, foreign_fields=None):
        result = []
        db_module = db_modules.get_db_module(db_type)
        result = db_module.create_table_sql(table_name, fields, gen_name, foreign_fields)
        return result

    def delete_table_sql(self, db_type):
        db_module = db_modules.get_db_module(db_type)
        gen_name = None
        if self.f_primary_key.value:
            gen_name = self.f_gen_name.value
        result = db_module.delete_table_sql(self.f_table_name.value, gen_name)
        return result

    def recreate_table_sql(self, db_type, old_fields, new_fields, fk_delta=None):

        def foreign_key_dict(ind):
            fields = ind.task.sys_fields.copy()
            fields.set_where(id=ind.f_foreign_field.value)
            fields.open()
            dic = {}
            dic['key'] = fields.f_db_field_name.value
            ref_id = fields.f_object.value
            items = self.task.sys_items.copy()
            items.set_where(id=ref_id)
            items.open()
            dic['ref'] = items.f_table_name.value
            primary_key = items.f_primary_key.value
            fields.set_where(id=primary_key)
            fields.open()
            dic['primary_key'] = fields.f_db_field_name.value
            return dic

        def get_foreign_fields():
            indices = self.task.sys_indices.copy()
            indices.set_where(owner_rec_id=self.id.value)
            indices.open()
            del_id = None
            if fk_delta and (fk_delta.rec_modified() or fk_delta.rec_deleted()):
                del_id = fk_delta.id.value
            result = []
            for ind in indices:
                if ind.f_foreign_index.value:
                    if not del_id or ind.id.value != del_id:
                        result.append(foreign_key_dict(ind))
            if fk_delta and (fk_delta.rec_inserted() or fk_delta.rec_modified()):
                result.append(foreign_key_dict(fk_delta))
            return result

        def create_indices_sql(db_type):
            indices = self.task.sys_indices.copy()
            indices.set_where(owner_rec_id=self.id.value)
            indices.open()
            result = []
            for ind in indices:
                if not ind.f_foreign_index.value:
                    result.append(ind.create_index_sql(db_type, self.f_table_name.value, new_fields=new_fields))
            return result

        def find_field(fields, id_value):
            found = False
            for f in fields:
                if f['id'] == id_value:
                    found = True
                    break
            return found

        def prepare_fields():
            for f in list(new_fields):
                if not find_field(old_fields, f['id']):
                    new_fields.remove(f)
            for f in list(old_fields):
                if not find_field(new_fields, f['id']):
                    old_fields.remove(f)

        table_name = self.f_table_name.value
        result = []
        result.append('PRAGMA foreign_keys=off')
        result.append('ALTER TABLE "%s" RENAME TO Temp' % table_name)
        foreign_fields = get_foreign_fields()
        create_sql = self.create_table_sql(db_type, table_name, new_fields, foreign_fields=foreign_fields)
        for sql in create_sql:
            result.append(sql)
        prepare_fields()
        old_field_list = ['"%s"' % field['field_name'] for field in old_fields]
        new_field_list = ['"%s"' % field['field_name'] for field in new_fields]
        result.append('INSERT INTO "%s" (%s) SELECT %s FROM Temp' % (table_name, ', '.join(new_field_list), ', '.join(old_field_list)))
        result.append('DROP TABLE Temp')
        result.append('PRAGMA foreign_keys=on')
        ind_sql = create_indices_sql(db_type)
        for sql in ind_sql:
            result.append(sql)
        return result

    def change_table_sql(self, db_type, old_fields, new_fields):

        def recreate(comp):
            for key, (old_field, new_field) in iteritems(comp):
                if old_field and new_field:
                    if old_field['field_name'] != new_field['field_name']:
                        return True
                    elif old_field['default_value'] != new_field['default_value']:
                        return True
                elif old_field and not new_field:
                    return True

        db_module = db_modules.get_db_module(db_type)
        table_name = self.f_table_name.value
        result = []
        comp = {}
        for field in old_fields:
            comp[field['id']] = [field, None]
        for field in new_fields:
            if comp.get(field['id']):
                comp[field['id']][1] = field
            else:
                if field['id']:
                    comp[field['id']] = [None, field]
                else:
                    comp[field['field_name']] = [None, field]
        if db_type == db_modules.SQLITE and recreate(comp):
            result += self.recreate_table_sql(db_type, old_fields, new_fields)
        else:
            for key, (old_field, new_field) in iteritems(comp):
                if old_field and not new_field and db_type != db_modules.SQLITE:
                    result.append(db_module.del_field_sql(table_name, old_field))
            for key, (old_field, new_field) in iteritems(comp):
                if old_field and new_field and db_type != db_modules.SQLITE:
                    if (old_field['field_name'] != new_field['field_name']) or \
                        (db_module.FIELD_TYPES[old_field['data_type']] != db_module.FIELD_TYPES[new_field['data_type']]) or \
                        (old_field['default_value'] != new_field['default_value']) or \
                        (old_field['size'] != new_field['size']):
                        sql = db_module.change_field_sql(table_name, old_field, new_field)
                        if type(sql) in (list, tuple):
                            result += sql
                        else:
                            result.append()
            for key, (old_field, new_field) in iteritems(comp):
                if not old_field and new_field:
                    result.append(db_module.add_field_sql(table_name, new_field))
        return result

    def create_index_sql(self, db_type, table_name, fields=None, new_fields=None, foreign_key_dict=None):

        def new_field_name_by_id(id_value):
            for f in new_fields:
                if f['id'] == id_value:
                    return f['field_name']

        db_module = db_modules.get_db_module(db_type)
        index_name = self.f_index_name.value
        if self.f_foreign_index.value:
            if foreign_key_dict:
                key = foreign_key_dict['key']
                ref = foreign_key_dict['ref']
                primary_key = foreign_key_dict['primary_key']
            else:
                fields = self.task.sys_fields.copy()
                fields.set_where(id=self.f_foreign_field.value)
                fields.open()
                key = fields.f_db_field_name.value
                ref_id = fields.f_object.value
                items = self.task.sys_items.copy()
                items.set_where(id=ref_id)
                items.open()
                ref = items.f_table_name.value
                primary_key = items.f_primary_key.value
                fields.set_where(id=primary_key)
                fields.open()
                primary_key = fields.f_db_field_name.value
            sql = db_module.create_foreign_index_sql(table_name, index_name, key, ref, primary_key)
        else:
            index_fields = self.f_fields_list.value
            desc = ''
            if self.descending.value:
                desc = 'DESC'
            unique = ''
            if self.f_unique_index.value:
                unique = 'UNIQUE'
            fields = self.load_index_fields(index_fields)
            if db_type == db_modules.FIREBIRD:
                if new_fields:
                    field_defs = [new_field_name_by_id(field[0]) for field in fields]
                else:
                    field_defs = [self.task.sys_fields.field_by_id(field[0], 'f_db_field_name') for field in fields]
                field_str = '"' + '", "'.join(field_defs) + '"'
            else:
                field_defs = []
                for field in fields:
                    if new_fields:
                        field_name = new_field_name_by_id(field[0])
                    else:
                        field_name = self.task.sys_fields.field_by_id(field[0], 'f_db_field_name')
                    d = ''
                    if field[1]:
                        d = 'DESC'
                    field_defs.append('"%s" %s' % (field_name, d))
                field_str = ', '.join(field_defs)
            sql = db_module.create_index_sql(index_name, table_name, unique, field_str, desc)
        return sql

    def delete_index_sql(self, db_type, table_name=None):
        db_module = db_modules.get_db_module(db_type)
        if not table_name:
            table_name = self.task.sys_items.field_by_id(self.owner_rec_id.value, 'f_table_name')
        index_name = self.f_index_name.value
        if self.f_foreign_index.value:
            sql = db_module.delete_foreign_index(table_name, index_name)
        else:
            sql = db_module.delete_index(table_name, index_name)
        return sql

    def load_interface(self):
        self._view_list = []
        self._edit_list = []
        self._order_list = []
        self._reports_list = []
        value = self.f_info.value
        if value:
            if len(value) >= 4 and value[0:4] == 'json':
                lists = json.loads(value[4:])
            else:
                lists = pickle.loads(to_bytes(value, 'utf-8'))
            self._view_list = lists['view']
            self._edit_list = lists['edit']
            self._order_list = lists['order']
            if lists.get('reports'):
                self._reports_list = lists['reports']

    def store_interface(self, connection=None):
        handlers = self.store_handlers()
        self.clear_handlers()
        try:
            self.edit()
            dic = {'view': self._view_list,
                    'edit': self._edit_list,
                    'order': self._order_list,
                    'reports': self._reports_list}
            self.f_info.value = 'json' + json.dumps(dic, default=json_defaul_handler)
            self.post()
            self.apply(connection)
        finally:
            handlers = self.load_handlers(handlers)

    def store_index_fields(self, f_list):
        return json.dumps(f_list)

    def load_index_fields(self, value):
        return json.loads(str(value))
