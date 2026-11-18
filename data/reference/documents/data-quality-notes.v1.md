---
doc_id: data-quality-notes
version: 1
title: Sales Data Quality Notes
owner: Data & Analytics
effective_date: 2026-01-01
synthetic: true
---

# Sales Data Quality Notes

These notes explain known characteristics of the sales data, so they are not mistaken for errors.

## Coverage

The operational sales tables cover 1 January 2026 to 30 April 2026. Earlier history is held in the legacy reporting system and is not available here.

## Missing customer IDs

An empty customer ID means a guest checkout: the customer did not use a loyalty account. It is not a data error. Guest orders are included in all sales metrics.

## Missing return reasons

Some returns have no reason recorded, typically when a return was processed at a busy till. These returns are still valid and included in refund value.

## Negative quantities

Negative quantities appear only on cancellation lines (invoice numbers starting with "C"). They are expected.

## Timestamps

All timestamps are in local store time. Orders placed just before midnight on the last day of a month belong to that month.

## What the data does not contain

The operational tables contain no product cost, profit, margin, web traffic, marketing spend or customer satisfaction data.
