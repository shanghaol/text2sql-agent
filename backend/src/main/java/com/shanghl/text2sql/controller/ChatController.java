package com.shanghl.text2sql.controller;

import java.io.IOException;

import com.shanghl.text2sql.dto.ApprovalRequest;
import com.shanghl.text2sql.dto.ChatRequest;
import com.shanghl.text2sql.security.CurrentUser;
import com.shanghl.text2sql.service.ChatProxyService;
import com.shanghl.text2sql.service.ChatSession;
import com.shanghl.text2sql.service.SseSink;

import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * POST /api/chat 与 POST /api/chat/approval（W5 §3.4）—— SSE 代理端点（设计 §2.5）。
 * HTTP 细节归属本类，业务逻辑全部在 ChatProxyService：
 * - 错误（限流/并发满/key 未设/上游不可达/审批不存在）发生在【流开始前】，
 *   由 GlobalExceptionHandler 以 JSON 返回（前端 fetch 先查 res.ok，§4.2 要点 1）；
 * - 上游 200 后才设 SSE 响应头并开始逐行透传；
 * - X-Accel-Buffering: no 与 Caddy 的 flush_interval -1 双保险禁缓冲（§2.5 步骤 6）。
 * SSE 是 POST + Authorization 头，无需 cookie（§2.3）；流建立后 token 过期不中断流。
 */
@RestController
public class ChatController {

    private final ChatProxyService chatProxyService;

    public ChatController(ChatProxyService chatProxyService) {
        this.chatProxyService = chatProxyService;
    }

    @PostMapping("/api/chat")
    public void chat(@Valid @RequestBody ChatRequest req,
                     @AuthenticationPrincipal CurrentUser user,
                     HttpServletResponse response) throws IOException {
        // open()：限流/并发闸/读 key/解密/连上游——任何失败此时都还未提交响应，走 JSON 错误路径
        ChatSession session = chatProxyService.open(user.id(), req.question(), req.threadId());
        streamToResponse(session, response, sink -> chatProxyService.streamAndPersist(session, sink));
    }

    /**
     * W5 §3.4：审批恢复端点——批准/拒绝待执行 SQL，Python resume 续跑，恢复流与 /api/chat
     * 同款透传；落库为**原地 UPDATE**（historyId 行 status=4 → 1/3，累加 latency/tokens）。
     */
    @PostMapping("/api/chat/approval")
    public void approval(@Valid @RequestBody ApprovalRequest req,
                         @AuthenticationPrincipal CurrentUser user,
                         HttpServletResponse response) throws IOException {
        // openApproval：限流/并发闸/审批原行归属与状态校验/读 key/连上游（流前错误走 JSON 路径）
        ChatSession session = chatProxyService.openApproval(user.id(), req);
        streamToResponse(session, response, sink -> chatProxyService.streamAndPersistForApproval(session, sink, req.decision()));
    }

    /** 共享的 SSE 响应头 + 流驱动骨架：streamed 标志兜底"流开始前"的意外异常（放闸）。 */
    private interface StreamBody {
        void run(SseSink sink) throws IOException;
    }

    private void streamToResponse(ChatSession session, HttpServletResponse response, StreamBody body) throws IOException {
        boolean streamed = false;
        try {
            response.setStatus(HttpServletResponse.SC_OK);
            response.setContentType("text/event-stream;charset=UTF-8");
            response.setHeader("Cache-Control", "no-cache");
            response.setHeader("X-Accel-Buffering", "no"); // 禁代理缓冲约定头（Caddy 侧另有 flush_interval）
            body.run(new ServletSseSink(response));
            streamed = true;
        } finally {
            if (!streamed) {
                session.close(); // streamAndPersist(ForApproval) 内部必 close；这里兜底流开始前的意外异常
            }
        }
    }
}
