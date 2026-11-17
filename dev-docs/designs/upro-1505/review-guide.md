# How to review this design

This review is meant to establish whether the proposed direction for the
end-to-end test work is sound. It is not a final review of every field,
validation case, tag spelling, or implementation choice.

Much of the epic will be implemented with help from an LLM. The design must
therefore give later work clear shared boundaries without trying to decide
every routine detail in advance.

## What is this review trying to establish?

The review should answer three broad questions:

1. Does the design take the suite in an appropriate overall direction?
2. Do the proposed parts have clear and sensible relationships?
3. Does the design make shared decisions precise while leaving local
   implementation choices open?

Reviewers are not limited to the concepts and components already named in the
documents. They should identify missing responsibilities, unnecessary parts,
or a different framing when those would produce a stronger design.

Review the proposed architecture and end goal instead of details of the
particular implementation steps.

## When does missing specificity matter?

The design needs enough specificity wherever separate implementation choices
must remain compatible. A missing detail matters when different reasonable
answers would:

* change the meaning of the suite;
* divide responsibility between components differently;
* discard information needed by later work;
* make two tools disagree about the same source;
* produce meaningfully different failure behavior.

This kind of gap may appear in a definition, a relationship, an ownership
boundary, an input or output, or an error path. The important question is not
whether every detail is written down. It is whether later work can make local
decisions without accidentally changing a shared decision.

## Which details can remain open?

The design does not need to enumerate every consequence of an accepted rule.
An implementation can fill in a routine detail when the design gives only one
reasonable answer.

Examples that normally do not block this review include:

* one missing good or bad example when the rule itself is clear;
* an unlisted malformed input already covered by a general validation rule;
* exact class, function, field, or command names;
* exact error wording;
* the serialized layout of information whose meaning is already defined;
* implementation tests for straightforward applications of an accepted rule.

These omissions can still produce useful review comments. Reviewers should
distinguish a detail that would improve the document from a decision that
several parts of the system must share.

## How should reviewers read the documents?

1. Read
   [`e2e_test_suite.md`](../../reference/e2e_test_suite.md)
   for the proposed vocabulary and maintainer-facing model. This is a
   human-facing document and should be judged according to that standard.
2. Read
   [`e2e_test_coverage.md`](../../explanation/e2e_test_coverage.md)
   for how current cases, coverage intent, and applicability are compared.
   This is also a human-facing document and should be judged according to that
   standard.
3. Read [`system-overview.md`](system-overview.md) for the flow from feature
   files to validation, coverage analysis, and execution. This is a
   reviewer-facing document created to help the team understand the work. It
   is not going to persist past this implementation. Review this for direction
   and clarity, but keep in mind that it's a temporary doc.
4. Use
   [`implementation-outline.md`](implementation-outline.md)
   when a decision needs its evidence, edge cases, or detailed rules. This is
   another reviewer-facing (and LLM-facing) document created to help the team
   understand the work. Review this for direction and clarity.
5. Test the design mentally against real feature files and the work that later
   tools might need to perform.
