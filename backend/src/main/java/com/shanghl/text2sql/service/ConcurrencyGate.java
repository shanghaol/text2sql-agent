package com.shanghl.text2sql.service;

import java.util.concurrent.atomic.AtomicInteger;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import com.shanghl.text2sql.config.AppProperties;

/**
 * 全局并发闸（设计 §1.7）：在流式对话的整个生命周期内占一个名额。
 * 2 核 CPU 上每条流 = 模型 token 吞吐 + 检索 + MySQL 查询；4 并发是 P95 延迟不劣化的
 * 保守上界（部署验收实测复核）。超出 → 503 concurrency_full。
 *
 * 纪律：tryEnter() 成功后【必须】恰好调用一次 exit()——ChatProxyService 用 ChatSession.close()
 * 保证（流异常、客户端断连、正常结束都走 finally 关闭），流异常也要放闸（§2.5 步骤 1）。
 */
@Component
public class ConcurrencyGate {

    private final AtomicInteger inFlight = new AtomicInteger();
    private final int max;

    /** Spring 装配入口：上限来自 app.chat.max-concurrent-streams（默认 4，§1.7）。 */
    @Autowired
    public ConcurrencyGate(AppProperties props) {
        this(props.getChat().getMaxConcurrentStreams());
    }

    public ConcurrencyGate(int max) {
        this.max = max;
    }

    /** 原子占位：返回 false 表示已满（内部已自减，调用方直接拒绝即可）。 */
    public boolean tryEnter() {
        int n = inFlight.incrementAndGet();
        if (n > max) {
            inFlight.decrementAndGet();
            return false;
        }
        return true;
    }

    /** 释放名额。只允许对成功 tryEnter 的调用方调用一次（ChatSession.close 有 closed 标志防重复）。 */
    public void exit() {
        inFlight.decrementAndGet();
    }

    /** 当前在流数（监控/测试用）。 */
    public int inFlight() {
        return inFlight.get();
    }

    public int max() {
        return max;
    }
}
