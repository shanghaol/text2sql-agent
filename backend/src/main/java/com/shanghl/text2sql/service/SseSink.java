package com.shanghl.text2sql.service;

import java.io.IOException;

/**
 * SSE 下行写出抽象（设计 §2.5 步骤 5：逐行写 + 每事件 flush）。
 * 生产实现是 ServletSseSink（servlet 输出流）；单测用内存 sink 捕获行、
 * 或用"写到第 N 行抛 IOException"的 sink 模拟客户端断连（ClientAbortException 是 IOException 子类）。
 */
public interface SseSink {

    /** 写一行（含换行）。客户端断连时实现类抛 IOException。 */
    void writeLine(String line) throws IOException;

    /** 冲刷。SSE 以空行分事件，ChatProxyService 在每个空行后调用，保证 delta 到达即渲染（§9.2-7）。 */
    void flush() throws IOException;
}
