# ministore on-disk format

Two files make up a database: the data file at the path given to
`kv_open`, and the log at `<path>.wal`.  Everything is little endian and
is written as packed structures; there is no version negotiation beyond
the magic and version fields in the meta page.

## Data file

The data file is an array of 1024 byte pages.  Page 0 is the meta page;
every other page is a leaf or an internal node.

### Page header (16 bytes, every page)

| offset | size | field  | meaning                                        |
| ------ | ---- | ------ | ---------------------------------------------- |
| 0      | 8    | lsn    | log sequence number of the newest change applied to this page |
| 8      | 2    | type   | 1 meta, 2 leaf, 3 internal                     |
| 10     | 2    | nkeys  | live entries in the page                       |
| 12     | 4    | link   | leaf: next leaf page; internal: leftmost child |

The page LSN is what makes redo repeatable: recovery applies a record
only when the page LSN is older than the record.

### Meta page (page 0)

Header, then:

| offset | size | field           | meaning                            |
| ------ | ---- | --------------- | ---------------------------------- |
| 16     | 4    | magic           | 0x4D53544F ("MSTO")                |
| 20     | 4    | version         | 3                                  |
| 24     | 4    | page_size       | 1024                               |
| 28     | 4    | num_pages       | pages in the data file             |
| 32     | 4    | root            | page id of the root node           |
| 36     | 4    | height          | 1 when the root is a leaf          |
| 40     | 8    | checkpoint_lsn  | recovery starts after this record  |
| 48     | 8    | next_lsn        | next log sequence number to hand out |

### Leaf page

Header, then up to 15 slots of 64 bytes each:

| offset | size | field |
| ------ | ---- | ----- |
| 0      | 8    | key   |
| 8      | 2    | vlen  |
| 10     | 2    | pad   |
| 12     | 52   | value |

Slots are sorted by key.  `link` points at the next leaf, or 0xFFFFFFFF
for the last one, so the whole database can be scanned in key order
without touching an internal node.

### Internal page

Header, then up to 63 entries of 16 bytes each:

| offset | size | field |
| ------ | ---- | ----- |
| 0      | 8    | key   |
| 8      | 4    | child |
| 12     | 4    | pad   |

Entries are sorted by key.  `link` is the leftmost child: keys below
`entry[0].key` live there, and keys at or above `entry[i].key` live in
`entry[i].child`.

## Log file

The log is a sequence of records, each a 48 byte header followed by
`vlen` payload bytes.

| offset | size | field | meaning                                       |
| ------ | ---- | ----- | --------------------------------------------- |
| 0      | 4    | magic | 0x57414C31 ("WAL1")                           |
| 4      | 4    | len   | header + payload                              |
| 8      | 4    | crc   | CRC32 of everything after this field          |
| 12     | 4    | type  | record type, below                            |
| 16     | 8    | lsn   | log sequence number                           |
| 24     | 8    | txn   | transaction this record belongs to            |
| 32     | 4    | page  | the page the record changes                   |
| 36     | 4    | aux   | a second page id, meaning depends on the type |
| 40     | 8    | key   | key or separator                              |
| 48     | 4    | arg   | split position, or a child page id            |
| 52     | 4    | vlen  | payload length                                |

A record whose magic, length or CRC does not check out marks the end of
the usable log: that is the torn tail a crash leaves behind, and
recovery stops there.

### Record types

| type | name       | meaning                                                       |
| ---- | ---------- | ------------------------------------------------------------- |
| 1    | BEGIN      | opens a transaction                                            |
| 2    | COMMIT     | closes it; only committed transactions are replayed            |
| 3    | LEAF_PUT   | insert or replace `key` on page `page`; payload is the value   |
| 4    | LEAF_DEL   | remove `key` from page `page`                                  |
| 5    | LEAF_SPLIT | page `page` keeps its first `arg` slots and links to `aux`; the slots from `arg` onwards belong to `aux` |
| 6    | INT_INSERT | insert separator `key` with child `arg` into page `page`       |
| 7    | INT_SPLIT  | page `page` keeps its first `arg` entries; the separator at `arg` moves up and the entries after it belong to `aux` |
| 8    | NEW_ROOT   | build a new root on page `page` with leftmost child `aux` and one entry (`key`, `arg`), and make it the root |
| 9    | CHECKPOINT | every page older than this record is on disk                   |

A record never carries more than a value: the largest payload is
`KV_MAX_VALUE_LEN` bytes.  Split records therefore describe the split
rather than the pages it produced.

The log is recycled at every checkpoint.  Once `kv_checkpoint` has
flushed the pages and recorded `checkpoint_lsn` in the meta page, the
records before that point can never be needed again, so the log file is
truncated and the next record starts at offset 0 with the next sequence
number.  A database that has just been closed therefore has an empty
log.

## Invariants a healthy database satisfies

* every page except the meta page is reachable from the root exactly once;
* keys inside a leaf are sorted and lie inside the range its ancestors imply;
* separators inside an internal page are sorted;
* walking the leaf chain visits every leaf, in key order, and yields the
  same number of keys as walking the tree;
* the log has no readable record newer than the last committed one.

`kv_verify` (and `kvcli verify`) checks all of these.
