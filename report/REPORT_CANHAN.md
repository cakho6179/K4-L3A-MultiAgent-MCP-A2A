# Báo cáo cá nhân — K4 L3A Multi-Agent MCP + A2A

| Mục | Nội dung |
| --- | --- |
| Họ tên | _(điền họ tên)_ |
| Mã học viên | _(điền mã HV)_ |
| Lớp | H202 |
| Team | h202-02881 — Cá Khô |
| Repo | https://github.com/cakho6179/K4-L3A-MultiAgent-MCP-A2A |
| Variant | l3a (`l3a-competition-v1`) |
| Điểm public | 92.979 — hạng 39/250 (leaderboard L3A) |

Ảnh điểm public: chụp tại workspace `/l3a` (bảng breakdown 7 thành phần) và
leaderboard. Công cụ không chụp màn hình thay được — mở workspace, bấm vào
dòng team để hiện breakdown, chụp lại 2 ảnh.

## 1. Bài toán

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử (100 case).
Mỗi case gồm `claimed_order_id`, 1–2 claim và `policy_version`. Agent phải lấy
dữ liệu thẩm quyền qua MCP Evidence Gateway (10 tool: order, item, payment,
shipment, seller, policy...), không được tin customer message, không được bịa
`evidence_ref`. Output và trace phải đúng public contract (7 tiêu chí chấm,
nặng nhất `semantic` 45%).

## 2. Kiến trúc đã triển khai

```text
inputs/<case_id>.json
  → coordinator ──task_assigned──┬─► order-agent     get_order, get_order_items
                                 ├─► payment-agent   get_order_payments, get_payment_timeline,
                                 │                   get_refund_timeline
                                 ├─► shipment-agent  get_shipment_summary, get_sellers
                                 └─► policy-agent    get_policy
  → AuthoritativeView (lọc decoy) → policy engine ──policy_decided──► verifier
  → outputs/<case_id>.json + traces/trace.jsonl (20 events/case)
```

- Mỗi specialist chỉ được gọi tool trong scope khai báo tại `AGENT_TOOLS`
  (`workflow.py`); gọi ngoài scope raise `ToolPermissionError`. Verifier không
  được gọi tool.
- Handoff tuyến tính một chiều `order → payment → shipment → policy →
  verifier`, tương quan bằng `case_id` — không có vòng lặp.
- Không dùng LLM ở runtime: pipeline async thuần Python, tất định.

## 3. Lọc decoy (quyết định chính)

Generator gắn mỗi case 1 kịch bản thẩm quyền + 1 decoy lệch mốc thời gian.
Bốn mỏ neo giữ bản thẩm quyền, phần loại bỏ ghi vào `data_conflicts`:

| Nguồn | Quy tắc |
| --- | --- |
| `order_items` | `shipping_limit_date` nhỏ nhất mà `>= order_purchase_timestamp` |
| `payment_timeline` | `event_at` cùng ngày lịch với `order_approved_at` |
| `refund_timeline` | `amount_brl` khớp capture thẩm quyền và `event_at >= order_approved_at` |
| `shipment.events` | `event_at == order_delivered_customer_date` |

## 4. Phân loại và hiệu chuẩn

`policy.classify()` theo thứ tự ưu tiên: canceled/unavailable + capture →
`reconciliation_mismatch` → refund failed/pending → duplicate/split (so
`paid_total` với `order_total`) → giao trễ (theo `actor` seller/logistics,
fallback `carrier_at > shipping_limit_at`) → `unsupported_claim` →
`insufficient_evidence`. Tiền hoàn, `case_status`, action và `party_type` tra
thẳng từ `get_policy`; riêng `party_id` của seller ràng buộc lại về seller của
chính order.

Confidence theo độ quyết định của luật thắng: 0.93 (trạng thái tường minh),
0.92 (event tường minh), 0.90 (giao trễ), 0.87 (suy luận số học), 0.78
(unsupported), 0.55 (thiếu evidence); hạ trần khi thiếu evidence/seller mơ hồ.

`verifier.verify()` cưỡng chế: refund ≥ 0, `no_action` không mang tiền,
`Σ refund_lines == recommended_refund_brl`, seller thuộc order, claim refs ⊆
output refs.

## 5. Thực nghiệm

- 100/100 cases chạy thật qua MCP (800 calls), `day09 validate` OK.
- Phân bố kết quả: đúng 10 cases cho mỗi loại trong 10 `primary_issue`
  (không có `insufficient_evidence`); 70 `action_required` hoàn tiền,
  20 `no_action`, 10 `needs_investigation`; 212 `data_conflicts`; confidence
  trung bình 0.894.
- Trace 2000 events: đủ `case_received`, `task_assigned`, `handoff`,
  `tool_result_consumed`, `policy_decided`, `verification_completed`,
  `case_finalized`.
- Test local: `pytest` pass, mock 8/8 kịch bản (gồm decoy) đúng schema.

## 6. Khó khăn và cách xử lý

1. MCP chỉ trả evidence sau khi tạo run (`POST /api/v2/runs`); `list_tools`
   thì public nên dễ nhầm key hỏng. Đã tạo run trước khi chạy bulk.
2. Server flaky theo đợt (`Error executing tool`, `ConnectError`): đổi retry
   sang retry cả `RuntimeError` 3 lần backoff + script resume nhiều pass, dọn
   orphan trace giữ lifecycle sạch.
3. `evidence_ref` xoay vòng mỗi run: toàn bộ 800 refs phải cùng một run nên
   không tạo run mới giữa chừng.
4. Bug `selected_source` ghi chuỗi `"None"` thay vì `null`: đã sửa và vá
   10 outputs.

## 7. Tái lập

```bash
python -m pip install -e ".[dev]"
cp .env.example .env   # điền COMPETITION_TEAM_API_KEY thật
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

## 8. Hướng cải tiến

Dựa vào breakdown 7 thành phần public: đẩy confidence nhóm tường minh lên
sát xác suất đúng thực tế (calibration), rà soát `EVIDENCE_PLAN` theo nhóm
thiếu coverage (evidence F1), và bổ sung cause phụ vào `ranked_causes` nếu
semantic còn trừ.
