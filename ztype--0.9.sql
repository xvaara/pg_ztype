\echo Use "CREATE EXTENSION ztype" to load this file. \quit

-- Fixed installation schema: C dictionary lookup uses ztype.dictionaries.
CREATE SCHEMA ztype;
REVOKE ALL ON SCHEMA ztype FROM PUBLIC;
GRANT USAGE ON SCHEMA ztype TO PUBLIC;

-- Typmod input resolves dictionary names against the admin-only registry, so it
-- runs as the extension owner and is STABLE, not IMMUTABLE: the same name maps to a
-- slot only within one snapshot. Only the slot number ever reaches the typmod.
CREATE FUNCTION ztext_typmod_in(cstring[]) RETURNS integer
  AS 'MODULE_PATHNAME', 'ztext_typmod_in' LANGUAGE C STABLE STRICT PARALLEL SAFE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_typmod_out(integer) RETURNS cstring
  AS 'MODULE_PATHNAME', 'ztext_typmod_out' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;

-- Same-type coercion: PostgreSQL applies the column's modifier through this cast
-- whenever a value's modifier differs, including literals and untyped parameters
-- (read with typmod -1 first), column-to-column moves and ALTER COLUMN TYPE.
-- The decoding entry points cost about 25 simple operators per call (a 2-3 us
-- decompression against cpu_operator_cost).

-- ANALYZE decodes each sample value once (never one declared wider than 1 kB) and runs the
-- base type's analyzer over the decoded values, so statistics cost what they cost for the base
-- type; the most-common values are stored re-encoded under the column's policy.
-- ztext: SQL transports logical values; compressed bytes never enter from clients.
CREATE TYPE ztext;
CREATE FUNCTION ztext_in(cstring, oid, integer) RETURNS ztext
  AS 'MODULE_PATHNAME', 'ztext_in' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_out(ztext) RETURNS cstring
  AS 'MODULE_PATHNAME', 'ztext_out' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_recv(internal, oid, integer) RETURNS ztext
  AS 'MODULE_PATHNAME', 'ztext_recv' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_send(ztext) RETURNS bytea
  AS 'MODULE_PATHNAME', 'ztext_send' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_typanalyze(internal) RETURNS boolean
  AS 'MODULE_PATHNAME', 'ztext_typanalyze' LANGUAGE C STRICT;
CREATE TYPE ztext (
  INPUT = ztext_in, OUTPUT = ztext_out, RECEIVE = ztext_recv, SEND = ztext_send,
  TYPMOD_IN = ztext_typmod_in, TYPMOD_OUT = ztext_typmod_out,
  ANALYZE = ztext_typanalyze, INTERNALLENGTH = VARIABLE, STORAGE = external, ALIGNMENT = int4);
CREATE FUNCTION ztext_to_text(ztext) RETURNS text
  AS 'MODULE_PATHNAME', 'ztext_to_text' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION text_to_ztext(text, integer, boolean) RETURNS ztext
  AS 'MODULE_PATHNAME', 'text_to_ztext' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext(ztext, integer, boolean) RETURNS ztext
  AS 'MODULE_PATHNAME', 'ztype_coerce' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE CAST (ztext AS text) WITH FUNCTION ztext_to_text(ztext) AS ASSIGNMENT;
CREATE CAST (text AS ztext) WITH FUNCTION text_to_ztext(text, integer, boolean) AS ASSIGNMENT;
CREATE CAST (ztext AS ztext) WITH FUNCTION ztext(ztext, integer, boolean) AS IMPLICIT;
CREATE FUNCTION ztype.recompress(ztext, level integer DEFAULT 6, slot integer DEFAULT 0) RETURNS ztext
  AS 'MODULE_PATHNAME', 'ztype_recompress' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.inspect(ztext, OUT kind text, OUT codec text, OUT level integer, OUT format integer,
  OUT raw_length integer, OUT stored_bytes integer, OUT dict_id bigint, OUT dict_slot integer, OUT dict_name text)
  AS 'MODULE_PATHNAME', 'ztype_inspect' LANGUAGE C STABLE STRICT PARALLEL SAFE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION raw_length(ztext) RETURNS integer
  AS 'MODULE_PATHNAME', 'ztype_raw_length' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
-- Integrity sweep: NULL when the value passes every check, otherwise the message the
-- decoding cast would raise. Soft errors, so it never aborts the sweeping transaction.
CREATE FUNCTION ztype.validate(ztext) RETURNS text
  AS 'MODULE_PATHNAME', 'ztype_validate_text' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
-- Catch-up predicate: does the stored value carry (level, slot)? Level equal, and either raw or a
-- frame naming the slot's dictionary. Envelope and frame header only, no decompression.
CREATE FUNCTION ztype.matches_policy(ztext, level integer, slot integer) RETURNS boolean
  AS 'MODULE_PATHNAME', 'ztype_matches_policy' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;

-- zjsonb: SQL transports logical values; compressed bytes never enter from clients.
CREATE TYPE zjsonb;
CREATE FUNCTION zjsonb_in(cstring, oid, integer) RETURNS zjsonb
  AS 'MODULE_PATHNAME', 'zjsonb_in' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_out(zjsonb) RETURNS cstring
  AS 'MODULE_PATHNAME', 'zjsonb_out' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_recv(internal, oid, integer) RETURNS zjsonb
  AS 'MODULE_PATHNAME', 'zjsonb_recv' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_send(zjsonb) RETURNS bytea
  AS 'MODULE_PATHNAME', 'zjsonb_send' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_typanalyze(internal) RETURNS boolean
  AS 'MODULE_PATHNAME', 'zjsonb_typanalyze' LANGUAGE C STRICT;
CREATE TYPE zjsonb (
  INPUT = zjsonb_in, OUTPUT = zjsonb_out, RECEIVE = zjsonb_recv, SEND = zjsonb_send,
  TYPMOD_IN = ztext_typmod_in, TYPMOD_OUT = ztext_typmod_out,
  ANALYZE = zjsonb_typanalyze, INTERNALLENGTH = VARIABLE, STORAGE = external, ALIGNMENT = int4);
CREATE FUNCTION zjsonb_to_jsonb(zjsonb) RETURNS jsonb
  AS 'MODULE_PATHNAME', 'zjsonb_to_jsonb' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION jsonb_to_zjsonb(jsonb, integer, boolean) RETURNS zjsonb
  AS 'MODULE_PATHNAME', 'jsonb_to_zjsonb' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb(zjsonb, integer, boolean) RETURNS zjsonb
  AS 'MODULE_PATHNAME', 'ztype_coerce' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE CAST (zjsonb AS jsonb) WITH FUNCTION zjsonb_to_jsonb(zjsonb) AS ASSIGNMENT;
CREATE CAST (jsonb AS zjsonb) WITH FUNCTION jsonb_to_zjsonb(jsonb, integer, boolean) AS ASSIGNMENT;
CREATE CAST (zjsonb AS zjsonb) WITH FUNCTION zjsonb(zjsonb, integer, boolean) AS IMPLICIT;
CREATE FUNCTION ztype.recompress(zjsonb, level integer DEFAULT 6, slot integer DEFAULT 0) RETURNS zjsonb
  AS 'MODULE_PATHNAME', 'ztype_recompress' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.inspect(zjsonb, OUT kind text, OUT codec text, OUT level integer, OUT format integer,
  OUT raw_length integer, OUT stored_bytes integer, OUT dict_id bigint, OUT dict_slot integer, OUT dict_name text)
  AS 'MODULE_PATHNAME', 'ztype_inspect' LANGUAGE C STABLE STRICT PARALLEL SAFE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
-- zjsonb validates its envelope and frame; the jsonb container inside is never walked.
CREATE FUNCTION ztype.validate(zjsonb) RETURNS text
  AS 'MODULE_PATHNAME', 'ztype_validate_jsonb' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.matches_policy(zjsonb, level integer, slot integer) RETURNS boolean
  AS 'MODULE_PATHNAME', 'ztype_matches_policy' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;

-- zbytea: SQL transports logical values; compressed bytes never enter from clients.
CREATE TYPE zbytea;
CREATE FUNCTION zbytea_in(cstring, oid, integer) RETURNS zbytea
  AS 'MODULE_PATHNAME', 'zbytea_in' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_out(zbytea) RETURNS cstring
  AS 'MODULE_PATHNAME', 'zbytea_out' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_recv(internal, oid, integer) RETURNS zbytea
  AS 'MODULE_PATHNAME', 'zbytea_recv' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_send(zbytea) RETURNS bytea
  AS 'MODULE_PATHNAME', 'zbytea_send' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_typanalyze(internal) RETURNS boolean
  AS 'MODULE_PATHNAME', 'zbytea_typanalyze' LANGUAGE C STRICT;
CREATE TYPE zbytea (
  INPUT = zbytea_in, OUTPUT = zbytea_out, RECEIVE = zbytea_recv, SEND = zbytea_send,
  TYPMOD_IN = ztext_typmod_in, TYPMOD_OUT = ztext_typmod_out,
  ANALYZE = zbytea_typanalyze, INTERNALLENGTH = VARIABLE, STORAGE = external, ALIGNMENT = int4);
CREATE FUNCTION zbytea_to_bytea(zbytea) RETURNS bytea
  AS 'MODULE_PATHNAME', 'zbytea_to_bytea' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION bytea_to_zbytea(bytea, integer, boolean) RETURNS zbytea
  AS 'MODULE_PATHNAME', 'bytea_to_zbytea' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea(zbytea, integer, boolean) RETURNS zbytea
  AS 'MODULE_PATHNAME', 'ztype_coerce' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE CAST (zbytea AS bytea) WITH FUNCTION zbytea_to_bytea(zbytea) AS ASSIGNMENT;
CREATE CAST (bytea AS zbytea) WITH FUNCTION bytea_to_zbytea(bytea, integer, boolean) AS ASSIGNMENT;
CREATE CAST (zbytea AS zbytea) WITH FUNCTION zbytea(zbytea, integer, boolean) AS IMPLICIT;
CREATE FUNCTION ztype.recompress(zbytea, level integer DEFAULT 6, slot integer DEFAULT 0) RETURNS zbytea
  AS 'MODULE_PATHNAME', 'ztype_recompress' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.inspect(zbytea, OUT kind text, OUT codec text, OUT level integer, OUT format integer,
  OUT raw_length integer, OUT stored_bytes integer, OUT dict_id bigint, OUT dict_slot integer, OUT dict_name text)
  AS 'MODULE_PATHNAME', 'ztype_inspect' LANGUAGE C STABLE STRICT PARALLEL SAFE SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION raw_length(zbytea) RETURNS integer
  AS 'MODULE_PATHNAME', 'ztype_raw_length' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION ztype.validate(zbytea) RETURNS text
  AS 'MODULE_PATHNAME', 'ztype_validate_bytea' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.matches_policy(zbytea, level integer, slot integer) RETURNS boolean
  AS 'MODULE_PATHNAME', 'ztype_matches_policy' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;

CREATE FUNCTION prefix(ztext, integer) RETURNS text
  AS 'MODULE_PATHNAME', 'ztype_prefix' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_object_field(zjsonb, text) RETURNS jsonb
  AS 'MODULE_PATHNAME', 'zjsonb_object_field' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_object_field_text(zjsonb, text) RETURNS text
  AS 'MODULE_PATHNAME', 'zjsonb_object_field_text' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.zstd_version() RETURNS text
  AS 'MODULE_PATHNAME', 'ztype_zstd_version' LANGUAGE C IMMUTABLE PARALLEL SAFE;
-- The loaded library against the installed script: `library` should equal the version in
-- pg_extension, `magic` the name of the committed storage fixture, and zstd_runtime the
-- zstd_compiled it was built against (a newer runtime is fine, an older one is not).
CREATE FUNCTION ztype.build_info(OUT library text, OUT magic text, OUT jsonb_format integer,
  OUT zstd_compiled text, OUT zstd_runtime text)
  AS 'MODULE_PATHNAME', 'ztype_build_info' LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE OPERATOR -> (LEFTARG = zjsonb, RIGHTARG = text, FUNCTION = zjsonb_object_field);
CREATE OPERATOR ->> (LEFTARG = zjsonb, RIGHTARG = text, FUNCTION = zjsonb_object_field_text);
-- The remaining jsonb operators, as SQL functions the planner inlines: `doc @> x` becomes
-- `(doc::jsonb) @> x` before index matching, so an index on the cast expression is used, and
-- the cost is the cast's one decode. Predicates carry the base operators' selectivity.
CREATE FUNCTION zjsonb_array_element(zjsonb, integer) RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.->) $2);
CREATE OPERATOR -> (LEFTARG = zjsonb, RIGHTARG = integer, FUNCTION = zjsonb_array_element);
CREATE FUNCTION zjsonb_array_element_text(zjsonb, integer) RETURNS text LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.->>) $2);
CREATE OPERATOR ->> (LEFTARG = zjsonb, RIGHTARG = integer, FUNCTION = zjsonb_array_element_text);
CREATE FUNCTION zjsonb_extract_path(zjsonb, text[]) RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.#>) $2);
CREATE OPERATOR #> (LEFTARG = zjsonb, RIGHTARG = text[], FUNCTION = zjsonb_extract_path);
CREATE FUNCTION zjsonb_extract_path_text(zjsonb, text[]) RETURNS text LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.#>>) $2);
CREATE OPERATOR #>> (LEFTARG = zjsonb, RIGHTARG = text[], FUNCTION = zjsonb_extract_path_text);
CREATE FUNCTION zjsonb_exists(zjsonb, text) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.?) $2);
CREATE OPERATOR ? (LEFTARG = zjsonb, RIGHTARG = text, FUNCTION = zjsonb_exists, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION zjsonb_exists_any(zjsonb, text[]) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.?|) $2);
CREATE OPERATOR ?| (LEFTARG = zjsonb, RIGHTARG = text[], FUNCTION = zjsonb_exists_any, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION zjsonb_exists_all(zjsonb, text[]) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.?&) $2);
CREATE OPERATOR ?& (LEFTARG = zjsonb, RIGHTARG = text[], FUNCTION = zjsonb_exists_all, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION zjsonb_contains(zjsonb, jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.@>) $2);
CREATE OPERATOR @> (LEFTARG = zjsonb, RIGHTARG = jsonb, FUNCTION = zjsonb_contains, COMMUTATOR = <@, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION zjsonb_contained(zjsonb, jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.<@) $2);
CREATE OPERATOR <@ (LEFTARG = zjsonb, RIGHTARG = jsonb, FUNCTION = zjsonb_contained, COMMUTATOR = @>, RESTRICT = matchingsel, JOIN = matchingjoinsel);
-- Commuted forms, so a literal may stand on the left ('{"n": 1}' <@ doc).
CREATE FUNCTION jsonb_contained_z(jsonb, zjsonb) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1 OPERATOR(pg_catalog.<@) $2::jsonb);
CREATE OPERATOR <@ (LEFTARG = jsonb, RIGHTARG = zjsonb, FUNCTION = jsonb_contained_z, COMMUTATOR = @>, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION jsonb_contains_z(jsonb, zjsonb) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1 OPERATOR(pg_catalog.@>) $2::jsonb);
CREATE OPERATOR @> (LEFTARG = jsonb, RIGHTARG = zjsonb, FUNCTION = jsonb_contains_z, COMMUTATOR = <@, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION zjsonb_path_exists_opr(zjsonb, jsonpath) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.@?) $2);
CREATE OPERATOR @? (LEFTARG = zjsonb, RIGHTARG = jsonpath, FUNCTION = zjsonb_path_exists_opr, RESTRICT = matchingsel, JOIN = matchingjoinsel);
CREATE FUNCTION zjsonb_path_match_opr(zjsonb, jsonpath) RETURNS boolean LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ($1::jsonb OPERATOR(pg_catalog.@@) $2);
CREATE OPERATOR @@ (LEFTARG = zjsonb, RIGHTARG = jsonpath, FUNCTION = zjsonb_path_match_opr, RESTRICT = matchingsel, JOIN = matchingjoinsel);

-- Equality and hashing on the decoded value, the base type's own semantics (bytewise for
-- ztext, which is not collatable, and zbytea; structural for zjsonb), so values stored under
-- different policies compare equal. Identical stored bytes and, for ztext and zbytea, differing
-- declared lengths decide without decoding; everything else decodes both sides. No ordering:
-- these are hash operator classes, enough for DISTINCT, GROUP BY, UNION, IN, hash joins, hash
-- partitioning and hash indexes, and they give ANALYZE an equality operator for statistics.
CREATE FUNCTION ztext_eq(ztext, ztext) RETURNS boolean
  AS 'MODULE_PATHNAME', 'ztext_eq' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_ne(ztext, ztext) RETURNS boolean
  AS 'MODULE_PATHNAME', 'ztext_ne' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_hash(ztext) RETURNS integer
  AS 'MODULE_PATHNAME', 'ztext_hash' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztext_hash_extended(ztext, bigint) RETURNS bigint
  AS 'MODULE_PATHNAME', 'ztext_hash_extended' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE OPERATOR = (LEFTARG = ztext, RIGHTARG = ztext, FUNCTION = ztext_eq,
  COMMUTATOR = =, NEGATOR = <>, RESTRICT = eqsel, JOIN = eqjoinsel, HASHES);
CREATE OPERATOR <> (LEFTARG = ztext, RIGHTARG = ztext, FUNCTION = ztext_ne,
  COMMUTATOR = <>, NEGATOR = =, RESTRICT = neqsel, JOIN = neqjoinsel);
CREATE OPERATOR CLASS ztext_hash_ops DEFAULT FOR TYPE ztext USING hash AS
  OPERATOR 1 =, FUNCTION 1 ztext_hash(ztext), FUNCTION 2 ztext_hash_extended(ztext, bigint);
CREATE FUNCTION zjsonb_eq(zjsonb, zjsonb) RETURNS boolean
  AS 'MODULE_PATHNAME', 'zjsonb_eq' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_ne(zjsonb, zjsonb) RETURNS boolean
  AS 'MODULE_PATHNAME', 'zjsonb_ne' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_hash(zjsonb) RETURNS integer
  AS 'MODULE_PATHNAME', 'zjsonb_hash' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zjsonb_hash_extended(zjsonb, bigint) RETURNS bigint
  AS 'MODULE_PATHNAME', 'zjsonb_hash_extended' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE OPERATOR = (LEFTARG = zjsonb, RIGHTARG = zjsonb, FUNCTION = zjsonb_eq,
  COMMUTATOR = =, NEGATOR = <>, RESTRICT = eqsel, JOIN = eqjoinsel, HASHES);
CREATE OPERATOR <> (LEFTARG = zjsonb, RIGHTARG = zjsonb, FUNCTION = zjsonb_ne,
  COMMUTATOR = <>, NEGATOR = =, RESTRICT = neqsel, JOIN = neqjoinsel);
CREATE OPERATOR CLASS zjsonb_hash_ops DEFAULT FOR TYPE zjsonb USING hash AS
  OPERATOR 1 =, FUNCTION 1 zjsonb_hash(zjsonb), FUNCTION 2 zjsonb_hash_extended(zjsonb, bigint);
CREATE FUNCTION zbytea_eq(zbytea, zbytea) RETURNS boolean
  AS 'MODULE_PATHNAME', 'zbytea_eq' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_ne(zbytea, zbytea) RETURNS boolean
  AS 'MODULE_PATHNAME', 'zbytea_ne' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_hash(zbytea) RETURNS integer
  AS 'MODULE_PATHNAME', 'zbytea_hash' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION zbytea_hash_extended(zbytea, bigint) RETURNS bigint
  AS 'MODULE_PATHNAME', 'zbytea_hash_extended' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 50 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE OPERATOR = (LEFTARG = zbytea, RIGHTARG = zbytea, FUNCTION = zbytea_eq,
  COMMUTATOR = =, NEGATOR = <>, RESTRICT = eqsel, JOIN = eqjoinsel, HASHES);
CREATE OPERATOR <> (LEFTARG = zbytea, RIGHTARG = zbytea, FUNCTION = zbytea_ne,
  COMMUTATOR = <>, NEGATOR = =, RESTRICT = neqsel, JOIN = neqjoinsel);
CREATE OPERATOR CLASS zbytea_hash_ops DEFAULT FOR TYPE zbytea USING hash AS
  OPERATOR 1 =, FUNCTION 1 zbytea_hash(zbytea), FUNCTION 2 zbytea_hash_extended(zbytea, bigint);

-- Dictionaries are append-only extension data. Admin functions retain invoker privileges.
CREATE FUNCTION ztype.dict_id(bytea) RETURNS bigint
  AS 'MODULE_PATHNAME', 'zstd_dict_id' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE TABLE ztype.dictionaries (
  slot integer PRIMARY KEY CHECK (slot BETWEEN 1 AND 33554431),
  dict_id bigint UNIQUE NOT NULL CHECK (dict_id > 0 AND dict_id <= 4294967295),
  -- Names are typmod-addressable, so they must be unique and never all digits.
  name text NOT NULL UNIQUE CHECK (name <> '' AND name !~ '^[0-9]+$'),
  dict bytea NOT NULL CHECK (octet_length(dict) BETWEEN 8 AND 1048576),
  trained_from text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (dict_id = ztype.dict_id(dict))
)
-- Logical decoding runs the output function of every published value inside a historic snapshot,
-- and that snapshot only sees rows written by transactions marked as catalog-changing. Without
-- this reloption a walsender decoding a dictionary-compressed value cannot see the registry row
-- naming its dictionary and the whole subscription stalls on "dictionary ID ... is not available".
WITH (user_catalog_table = true);
SELECT pg_catalog.pg_extension_config_dump('ztype.dictionaries', '');
REVOKE ALL ON ztype.dictionaries FROM PUBLIC;
COMMENT ON TABLE ztype.dictionaries IS
  'Append-only trained dictionaries. Bytes may contain training data: do not grant public SELECT. '
  'Never disable the immutability triggers while stored values may depend on these rows.';

-- Inventory for ordinary roles: everything except the bytes and the training query.
CREATE VIEW ztype.dictionary_inventory AS
  SELECT slot, name, dict_id, octet_length(dict) AS dict_bytes, created_at FROM ztype.dictionaries;
GRANT SELECT ON ztype.dictionary_inventory TO PUBLIC;

-- Every stored ztext/zjsonb/zbytea column with its decoded modifier: the level, the
-- slot, and the dictionary the slot resolves to here (NULL for slot 0, and NULL with a
-- non-zero slot when the column asks for a dictionary this database does not have,
-- which is what a table-only restore or a lagging subscriber leaves behind).
-- A bare column (no modifier) shows the (6, 0) it applies to logical input. `pending` is
-- the state ztype.set_column_policy leaves behind until ztype.finish_column_policy: the
-- modifier is what new writes get, stored rows may still carry an earlier one.
CREATE VIEW ztype.column_policies AS
  SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name,
         t.typname AS type_name, a.atttypmod <> -1 AS has_modifier,
         CASE WHEN a.atttypmod = -1 THEN 6 ELSE a.atttypmod & 31 END AS level,
         CASE WHEN a.atttypmod = -1 THEN 0 ELSE (a.atttypmod >> 5) & 33554431 END AS slot,
         a.atttypmod <> -1 AND (a.atttypmod >> 30) & 1 = 1 AS pending,
         d.name AS dict_name, d.dict_id
    FROM pg_catalog.pg_attribute a
    JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
    LEFT JOIN ztype.dictionary_inventory d ON a.atttypmod <> -1 AND (a.atttypmod >> 5) & 33554431 > 0 AND d.slot = (a.atttypmod >> 5) & 33554431
   WHERE t.typname IN ('ztext', 'zjsonb', 'zbytea')
     AND t.typnamespace = (SELECT extnamespace FROM pg_catalog.pg_extension WHERE extname = 'ztype')
     AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'm');
GRANT SELECT ON ztype.column_policies TO PUBLIC;

-- Subscriber-side sweep: which of this database's columns store under a different policy than
-- the same column on a publisher. Takes the publisher's column_policies rows as one jsonb array
-- (SELECT jsonb_agg(p) FROM ztype.column_policies p, run there and carried over as text), joins
-- on schema, table and column, and returns the columns whose level or dictionary differ. The
-- dictionary is compared by ID, never by slot or name: a subscriber slot holding the publisher's
-- bytes under another number is not a difference. Replication itself never notices any of this
-- (values are re-encoded under the subscriber's policy and stay exact), which is why this exists.
CREATE FUNCTION ztype.policy_differences(publisher jsonb,
  OUT schema_name name, OUT table_name name, OUT column_name name, OUT type_name name,
  OUT local_level integer, OUT local_slot integer, OUT local_dict_name text, OUT local_dict_id bigint,
  OUT publisher_level integer, OUT publisher_slot integer, OUT publisher_dict_name text, OUT publisher_dict_id bigint,
  OUT difference text)
RETURNS SETOF record LANGUAGE sql STABLE STRICT PARALLEL SAFE SET search_path = pg_catalog, pg_temp AS $$
  SELECT l.schema_name, l.table_name, l.column_name, l.type_name,
         l.level, l.slot, l.dict_name, l.dict_id,
         p.level, p.slot, p.dict_name, p.dict_id,
         concat_ws(', ',
           CASE WHEN l.level <> p.level THEN 'different level' END,
           CASE WHEN l.dict_id IS DISTINCT FROM p.dict_id THEN
             CASE WHEN p.dict_id IS NULL THEN 'dictionary here, none on the publisher'
                  WHEN l.dict_id IS NULL AND l.slot > 0 THEN 'slot not registered here'
                  WHEN l.dict_id IS NULL THEN 'no dictionary here'
                  ELSE 'different dictionary' END END)
    FROM ztype.column_policies l
    JOIN jsonb_to_recordset(publisher) AS p(schema_name name, table_name name, column_name name,
                                            level integer, slot integer, dict_name text, dict_id bigint)
      ON p.schema_name = l.schema_name AND p.table_name = l.table_name AND p.column_name = l.column_name
   WHERE l.level <> p.level OR l.dict_id IS DISTINCT FROM p.dict_id
$$;

-- Blocking mutation also makes cache hits valid throughout a transaction.
CREATE FUNCTION ztype.reject_dictionary_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'ztype: dictionaries are append-only; add a new slot instead'
    USING ERRCODE = '55000';
END $$;
CREATE TRIGGER dictionaries_immutable
  BEFORE UPDATE OR DELETE OR TRUNCATE ON ztype.dictionaries
  FOR EACH STATEMENT EXECUTE FUNCTION ztype.reject_dictionary_mutation();
ALTER TABLE ztype.dictionaries ENABLE ALWAYS TRIGGER dictionaries_immutable;
CREATE FUNCTION ztype.train_dictionary(query text, dict_bytes integer DEFAULT 112640, sample_bytes integer DEFAULT 8192) RETURNS bytea
  AS 'MODULE_PATHNAME', 'zstd_train_dictionary' LANGUAGE C VOLATILE STRICT;
CREATE FUNCTION ztype.reload_dictionaries() RETURNS void
  AS 'MODULE_PATHNAME', 'ztype_reload_dictionaries' LANGUAGE C VOLATILE;
-- Backend-local cache accounting against ztype.dictionary_cache_size: live
-- entries and bytes, lifetime loads and evictions. Not parallel safe by design.
CREATE FUNCTION ztype.dictionary_cache_stats(OUT entries integer, OUT bytes bigint, OUT budget_bytes bigint,
  OUT loads bigint, OUT evictions bigint)
  AS 'MODULE_PATHNAME', 'ztype_dictionary_cache_stats' LANGUAGE C VOLATILE;
-- Backend-local decode cache counters: the last decoded compressed value is kept
-- so repeated references to one row decode once. Not parallel safe by design.
CREATE FUNCTION ztype.decode_cache_stats(OUT hits bigint, OUT misses bigint, OUT bytes bigint)
  AS 'MODULE_PATHNAME', 'ztype_decode_cache_stats' LANGUAGE C VOLATILE;

-- Serialize registrations so max(slot)+1 cannot race; slots never get reused.
-- SECURITY DEFINER (decided 2026-09-09): the body is fixed SQL over the registry with the
-- arguments as data, so EXECUTE on this function is the whole delegation grant, and a
-- delegated role never holds SELECT on the dictionary bytes. Training stays with caller
-- privileges: its query must not read what the caller cannot.
CREATE FUNCTION ztype.add_dictionary(name text, dict bytea, trained_from text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s integer;
BEGIN
  IF name IS NULL OR dict IS NULL THEN
    RAISE EXCEPTION 'ztype: dictionary name and bytes are required' USING ERRCODE = '22004';
  END IF;
  PERFORM ztype.dict_id(dict);
  LOCK TABLE ztype.dictionaries IN SHARE ROW EXCLUSIVE MODE;
  SELECT coalesce(max(slot), 0) + 1 INTO s FROM ztype.dictionaries;
  IF s > 33554431 THEN
    RAISE EXCEPTION 'ztype: all 33554431 dictionary slots are occupied' USING ERRCODE = '54000';
  END IF;
  INSERT INTO ztype.dictionaries (slot, dict_id, name, dict, trained_from)
  VALUES (s, ztype.dict_id(dict), name, dict, trained_from);
  RETURN s;
END $$;

-- Import with a preserved slot: the recovery and subscriber-seeding path, where the slot
-- must be the one the source's columns name. Idempotent on an identical row, and every
-- other overlap is refused with ztype's own 42710 naming what the target already has,
-- registry untouched. No table lock: the primary key serializes two imports of one slot,
-- and add_dictionary's SHARE ROW EXCLUSIVE lock already waits for an in-flight insert.
-- SECURITY DEFINER like add_dictionary: EXECUTE on it is the whole import grant, and the
-- collision check reads the inventory view, so no path here returns dictionary bytes.
CREATE FUNCTION ztype.dictionary_collision(slot integer, name text, dict_id bigint)
RETURNS boolean LANGUAGE plpgsql STABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
DECLARE have record;
BEGIN
  FOR have IN
    SELECT i.slot, i.name, i.dict_id FROM ztype.dictionary_inventory i
     WHERE i.slot = dictionary_collision.slot OR i.name = dictionary_collision.name
        OR i.dict_id = dictionary_collision.dict_id
  LOOP
    IF have.slot = dictionary_collision.slot AND have.name = dictionary_collision.name
       AND have.dict_id = dictionary_collision.dict_id THEN
      RETURN true;  -- the same dictionary is already there under the same slot and name
    ELSIF have.slot = dictionary_collision.slot THEN
      RAISE EXCEPTION 'ztype: slot % is taken by dictionary "%" (ID %)', have.slot, have.name, have.dict_id
        USING ERRCODE = '42710', HINT = 'Keep the existing row; register the incoming dictionary with ztype.add_dictionary under a free slot and point columns at it.';
    ELSIF have.dict_id = dictionary_collision.dict_id THEN
      RAISE EXCEPTION 'ztype: dictionary ID % is already registered as slot % ("%")', have.dict_id, have.slot, have.name
        USING ERRCODE = '42710', HINT = 'The bytes are already here; point columns at that slot.';
    ELSE
      RAISE EXCEPTION 'ztype: dictionary name "%" is already registered as slot % (ID %)', have.name, have.slot, have.dict_id
        USING ERRCODE = '42710', HINT = 'Keep the existing row; import the incoming dictionary under another name.';
    END IF;
  END LOOP;
  RETURN false;
END $$;
CREATE FUNCTION ztype.import_dictionary(slot integer, name text, dict bytea, trained_from text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE id bigint;
BEGIN
  IF slot IS NULL OR name IS NULL OR dict IS NULL THEN
    RAISE EXCEPTION 'ztype: dictionary slot, name and bytes are required' USING ERRCODE = '22004';
  END IF;
  IF slot < 1 OR slot > 33554431 THEN
    RAISE EXCEPTION 'ztype: dictionary slot must be between 1 and 33554431' USING ERRCODE = '22023';
  END IF;
  IF name = '' OR name ~ '^[0-9]+$' THEN
    RAISE EXCEPTION 'ztype: dictionary name must not be empty or all digits' USING ERRCODE = '22023';
  END IF;
  id := ztype.dict_id(dict);
  IF ztype.dictionary_collision(slot, name, id) THEN
    RETURN slot;
  END IF;
  BEGIN
    INSERT INTO ztype.dictionaries (slot, dict_id, name, dict, trained_from)
    VALUES (import_dictionary.slot, id, import_dictionary.name, import_dictionary.dict, import_dictionary.trained_from);
  EXCEPTION WHEN unique_violation THEN
    -- A concurrent import committed first. Under READ COMMITTED the recheck sees it and
    -- answers as above; under REPEATABLE READ it cannot, and the constraint's error stands.
    IF ztype.dictionary_collision(slot, name, id) THEN
      RETURN slot;
    END IF;
    RAISE;
  END;
  RETURN slot;
END $$;

-- Name lookup for migrations and recompression, callable by ordinary roles:
--   UPDATE t SET body = ztype.recompress(body, 6, ztype.dictionary_slot('mail-2024'));
CREATE FUNCTION ztype.dictionary_slot(dict_name text)
RETURNS integer LANGUAGE plpgsql STABLE STRICT PARALLEL SAFE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s integer;
BEGIN
  SELECT slot INTO s FROM ztype.dictionaries WHERE name = dict_name;
  IF s IS NULL THEN
    RAISE EXCEPTION 'ztype: dictionary "%" is not registered', dict_name USING ERRCODE = '42704';
  END IF;
  RETURN s;
END $$;

-- Rewrite-free policy change. set_column_policy stores the new modifier in pg_attribute, as
-- ALTER COLUMN TYPE would, and rewrites nothing: rows keep the policy they were written with, new
-- writes get the new one, and the modifier carries `pending` (ztext(9,2,pending)) until every row
-- has been caught up and finish_column_policy, under an ACCESS EXCLUSIVE lock, has checked that
-- with matches_policy and cleared it. Both run with the caller's rights and require ownership of
-- the table and of every partition or child; dictionary names resolve through dictionary_slot.
-- A view or rule on the column, or extended statistics, are refused like ALTER TABLE refuses a
-- view; expression indexes, CHECK constraints and trigger conditions are updated in place.
CREATE FUNCTION ztype.set_column_policy(tbl regclass, col name, level integer, dictionary text DEFAULT NULL)
RETURNS void AS 'MODULE_PATHNAME', 'ztype_set_column_policy' LANGUAGE C VOLATILE SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.set_column_policy(tbl regclass, col name, level integer, slot integer)
RETURNS void AS 'MODULE_PATHNAME', 'ztype_set_column_policy' LANGUAGE C VOLATILE SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.finish_column_policy(tbl regclass, col name)
RETURNS void AS 'MODULE_PATHNAME', 'ztype_finish_column_policy' LANGUAGE C VOLATILE STRICT SET search_path = pg_catalog, pg_temp;

CREATE FUNCTION ztype.train_and_add(name text, query text, dict_bytes integer DEFAULT 112640)
RETURNS integer LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $$
  SELECT ztype.add_dictionary(name, ztype.train_dictionary(query, dict_bytes), query)
$$;
REVOKE ALL ON FUNCTION ztype.dict_id(bytea),
  ztype.reject_dictionary_mutation(), ztype.train_dictionary(text, integer, integer),
  ztype.reload_dictionaries(), ztype.add_dictionary(text, bytea, text),
  ztype.import_dictionary(integer, text, bytea, text),
  ztype.train_and_add(text, text, integer) FROM PUBLIC;

-- Compressed output pass-through: the stored zstd frame without the envelope, always exactly
-- one frame (a raw-stored value is encoded at level 1 per call), for a client that decodes
-- zstd itself or forwards the bytes as Content-Encoding: zstd. Dictionary frames come back as
-- stored unless portable, which decodes and re-encodes without the dictionary at level 1. The
-- pass-through never decodes and never loads a dictionary; the content checksum is the
-- client decoder's to verify. SECURITY DEFINER like the casts, for the portable path alone:
-- a role that may decode a dictionary column may ask for a portable frame of it. No zjsonb
-- overload by decision (its payload is PostgreSQL's binary jsonb): ztype.zstd(doc::text).
CREATE FUNCTION ztype.zstd(ztext, portable boolean DEFAULT false) RETURNS bytea
  AS 'MODULE_PATHNAME', 'ztext_zstd' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
CREATE FUNCTION ztype.zstd(zbytea, portable boolean DEFAULT false) RETURNS bytea
  AS 'MODULE_PATHNAME', 'zbytea_zstd' LANGUAGE C STABLE STRICT PARALLEL SAFE COST 25 SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
-- Any base-type value as one dictionary-free frame at the given level, 1..22.
CREATE FUNCTION ztype.zstd(text, level integer DEFAULT 1) RETURNS bytea
  AS 'MODULE_PATHNAME', 'ztype_zstd_base' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25;
CREATE FUNCTION ztype.zstd(bytea, level integer DEFAULT 1) RETURNS bytea
  AS 'MODULE_PATHNAME', 'ztype_zstd_base' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE COST 25;
-- The zstd dictionary ID a stored frame names, NULL when it names none; envelope and frame
-- header only, so an application can key a dictionary cache on it before fetching the frame.
CREATE FUNCTION ztype.dictionary_id(ztext) RETURNS bigint
  AS 'MODULE_PATHNAME', 'ztext_dictionary_id' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION ztype.dictionary_id(zbytea) RETURNS bigint
  AS 'MODULE_PATHNAME', 'zbytea_dictionary_id' LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
-- The dictionary bytes by ID, NULL when the ID is not registered, so a client fetches each
-- dictionary once. SECURITY INVOKER by decision (2026-09-12): the bytes can contain training
-- data, so this reads the registry as the caller and hands them only to a role that holds
-- SELECT on ztype.dictionaries; EXECUTE alone, and any definer wrapper, would be a new path
-- to the bytes. Not granted to PUBLIC, like the other registry functions.
CREATE FUNCTION ztype.dictionary(dict_id bigint) RETURNS bytea
  LANGUAGE SQL STABLE STRICT PARALLEL SAFE SECURITY INVOKER SET search_path = pg_catalog, pg_temp
  BEGIN ATOMIC
    SELECT dict FROM ztype.dictionaries d WHERE d.dict_id = dictionary.dict_id;
  END;
REVOKE ALL ON FUNCTION ztype.dictionary(bigint) FROM PUBLIC;
