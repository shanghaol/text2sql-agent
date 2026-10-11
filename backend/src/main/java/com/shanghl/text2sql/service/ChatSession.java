package com.shanghl.text2sql.service;

import java.io.InputStream;
import java.net.http.HttpResponse;
import java.util.concurrent.atomic.AtomicBoolean;

import com.shanghl.text2sql.entity.QueryHistory;

/**
 * 一次对话代理的会话句柄：持有上游 SSE 流与并发闸名额。
 * close() 幂等（AtomicBoolean 防重）：关上游 InputStream（TCP 层向 Python 传 FIN/RST，
 * 其生成器收到 GeneratorExit 退出，§9.2-5）+ 释放并发闸（§2.5 步骤 1：流异常也要放闸）。
 */
public final class ChatSession implements AutoCloseable {

    private final long userId;
    private final String threadId;
    private final String question;
    private final long startMs;
    private final HttpResponse<InputStream> upstream;
    private final ConcurrencyGate gate;
    /** W5 HITL：审批流的原 query_history 行（status=4，openApproval 已校验归属）；普通对话流为 null。 */
    private final QueryHistory pendingHistory;
    private final AtomicBoolean closed = new AtomicBoolean(false);

    ChatSession(long userId, String threadId, String question, long startMs,
                HttpResponse<InputStream> upstream, ConcurrencyGate gate, QueryHistory pendingHistory) {
        this.userId = userId;
        this.threadId = threadId;
        this.question = question;
        this.startMs = startMs;
        this.upstream = upstream;
        this.gate = gate;
        this.pendingHistory = pendingHistory;
    }

    public long userId() { return userId; }
    public String threadId() { return threadId; }
    public String question() { return question; }
    public long startMs() { return startMs; }
    HttpResponse<InputStream> upstream() { return upstream; }
    QueryHistory pendingHistory() { return pendingHistory; }

    @Override
    public void close() {
        if (!closed.compareAndSet(false, true)) {
            return;
        }
        try {
            upstream.body().close(); // 关闭上游流 → 传导断开到 Python
        } catch (Exception e) {
            // 关闭失败不影响放闸
        }
        gate.exit();
    }
}
