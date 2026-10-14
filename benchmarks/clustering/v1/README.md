# Clustering benchmark v1 (T27)

200 hand-labelled articles for tuning §8 clustering and merge thresholds in Week 3.
See `docs/T27_ANNOTATION_GUIDE.md`.

```
items.jsonl              {id, url, title, snippet, publisher_group, source_type,
                          published_at, case, notes?}       — the 200 articles
labels/aaryan.jsonl      {id, event}                        — independent labels
labels/aaditya.jsonl     {id, event}                        — independent labels
labels/resolved.jsonl    {id, event, resolution?}           — both founders agree
candidates.jsonl         collector output; hints only, never labels (gitignored)
```

Bodies are not stored here (§7.4 content rights): items carry URLs and metadata.
