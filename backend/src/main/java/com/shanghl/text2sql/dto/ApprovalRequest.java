package com.shanghl.text2sql.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

/**
 * HITL 审批请求（W5 设计 §3.4）：POST /api/chat/approval 的 body。
 * - threadId：前端会话 uuid（Python 端拼 thread_key 用，校验同 /api/chat）；
 * - approvalId：pending_approval 事件下发的审批 id；
 * - decision：approve / reject（正则预检 + 服务端白名单双保险，非法 → 422）；
 * - historyId：pending_saved 事件绑定的历史行 id——批准/拒绝后**原地 UPDATE** 该行
 *   （status=4 → 1/3，W5 §3.6），不是新插入。
 */
public record ApprovalRequest(
        @NotBlank(message = "threadId 不能为空") String threadId,
        @NotBlank(message = "approvalId 不能为空") String approvalId,
        @NotBlank(message = "decision 不能为空")
        @Pattern(regexp = "approve|reject", message = "decision 仅支持 approve / reject") String decision,
        @NotNull(message = "historyId 不能为空") Long historyId) {
}
