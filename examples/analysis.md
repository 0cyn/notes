# Analysis notebook

Keep the **source inline** while using *live formatting*. Nested **bold and
*italic*** and Unicode — α, 日本語, 🙂 — should stay editable.

## Working notes

- [x] Identify the entry point
- [ ] Document the allocation path
- [ ] Follow up on ~~an old assumption~~ the updated ownership model

> Record assumptions alongside the evidence that supports them.

### Code and observations

The `allocation_size` variable controls the requested buffer size.

```python
def summarize(name, allocation_size):
    # Notes can include code without turning into a code editor.
    return f"{name}: {allocation_size:#x} bytes"
```

```c
struct observation {
    unsigned long address;
    const char *description;
};
```

| Topic | Status |
| --- | --- |
| Allocation | Documented |
| Ownership | Investigating |

## References

1. [Binary Ninja documentation](https://docs.binary.ninja/)
2. Save this note with the analysis database, or export a Markdown copy.

---

### Next session

Leave a short handoff here so the next analysis session starts with context.
