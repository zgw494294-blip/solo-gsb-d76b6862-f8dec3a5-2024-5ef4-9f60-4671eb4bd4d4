"use strict";

// 验证「补偿末条提交」的前端行为：
// 发送正整数 seq、成功渲染系统补偿记录并刷新已提交值、写锁冲突显示
// SQLite 错误名 / 码（时间线不变）、409（过期目标 / 脚本有效）显示服务端
// 说明并以服务端状态重核对、非法序号不发请求、新建进行中禁用、删除冻结、
// 脚本有效期间禁用、旧实验迟到响应不影响新实验。
// 响应顺序完全可控，不依赖真实时间与网络。
// 运行：node tests/compensate.test.js

const {
  loadApp,
  flush,
  bootOldLive,
  settleNewCreated,
} = require("./harness");

let passed = 0;
let failed = 0;

function assert(cond, message) {
  if (!cond) throw new Error(message);
}
function assertEq(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}（期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)})`);
  }
}
async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log("  ✓ " + name);
  } catch (e) {
    failed++;
    console.error("  ✗ " + name);
    console.error("      " + e.message);
  }
}

const REC1 = { seq: 1, side: "a", before: 100, after: 112, deltas: [5, 7] };
const REC2 = {
  seq: 2, side: "system", before: 112, after: 100,
  deltas: [-7, -5], compensates: 1,
};

function timelineTexts(ctx) {
  return ctx.el("timeline-list")._children.map((li) => li.textContent);
}

async function main() {
  // 补偿成功：请求体为正整数 seq，渲染系统补偿记录，刷新已提交值
  await test("补偿成功：发送 seq、渲染系统记录、刷新已提交值", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    assertEq(ctx.el("compensate-btn").disabled, false, "存活时补偿可用");

    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    const call = ctx.findCall("/OLD/compensate", "POST");
    assert(!!call, "应发出补偿请求");
    assertEq(call.body, JSON.stringify({ seq: 1 }), "补偿请求体");
    // 在途：补偿控件禁用
    assertEq(ctx.el("compensate-btn").disabled, true, "在途时按钮禁用");
    assertEq(ctx.el("compensate-seq").disabled, true, "在途时输入框禁用");

    call.settled = true;
    call.resolve({
      status: 200, ok: true,
      json: async () => ({ ok: true, error: null, record: REC2, timeline: [REC1, REC2] }),
    });
    await flush();
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 2, "渲染两条时间线记录");
    assert(texts[0].includes("#1") && texts[0].includes("甲（A）")
      && texts[0].includes("100 → 112") && texts[0].includes("[5, 7]"),
      "原记录照常渲染");
    assert(texts[1].includes("#2") && texts[1].includes("系统（SYSTEM）")
      && texts[1].includes("112 → 100") && texts[1].includes("[-7, -5]")
      && texts[1].includes("（补偿 #1）"), "系统补偿记录渲染");
    assertEq(
      ctx.el("timeline-list")._children[1].classList.contains("timeline-compensation"),
      true, "补偿记录带样式类");
    assertEq(ctx.el("error-timeline").hidden, true, "成功不显示错误");
    // 成功后刷新已提交值
    assert(!!ctx.findCall("/OLD/committed"), "补偿后应刷新已提交值");
    ctx.settle("/OLD/committed", 200, { committed: 100 });
    await flush();
    assertEq(ctx.el("committed").textContent, 100, "已提交值更新为 100");
    assertEq(ctx.el("compensate-btn").disabled, false, "完成后按钮恢复");
  });

  // 非法序号：不发请求，本地显示错误
  await test("非法序号：不发请求、本栏错误提示", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    for (const raw of ["", "abc", "0", "-3", "1.5", "1e2"]) {
      ctx.el("compensate-seq").value = raw;
      const before = ctx.calls.length;
      ctx.el("compensate-btn").click();
      await flush();
      assertEq(ctx.calls.length, before, `序号 ${JSON.stringify(raw)} 不得发请求`);
      assertEq(ctx.el("error-timeline").hidden, false, "显示非法输入错误");
    }
  });

  // 写锁冲突：200 + ok:false，显示 SQLITE_BUSY(5)，时间线不变
  await test("写锁冲突：显示 SQLite 错误名 / 码，时间线不变", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    const call = ctx.findCall("/OLD/compensate", "POST");
    call.settled = true;
    call.resolve({
      status: 200, ok: true,
      json: async () => ({
        ok: false,
        error: { name: "SQLITE_BUSY", code: 5, message: "database is locked" },
        timeline: [REC1],
      }),
    });
    await flush();
    assertEq(ctx.el("error-timeline").hidden, false, "显示写锁错误");
    assert(ctx.el("error-timeline").textContent.includes("SQLITE_BUSY"), "错误名");
    assert(ctx.el("error-timeline").textContent.includes("错误码 5"), "错误码");
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 1, "时间线不变（仍 1 条）");
    assertEq(ctx.el("compensate-btn").disabled, false, "失败后按钮恢复");
  });

  // 409 过期目标：显示服务端说明，并立即拉状态重核对
  await test("409 过期目标：显示服务端说明并以服务端状态重核对", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    const call = ctx.findCall("/OLD/compensate", "POST");
    call.settled = true;
    call.resolve({
      status: 409, ok: false,
      json: async () => ({ error: "stale target: only the last record (seq 2) can be compensated" }),
    });
    await flush();
    assertEq(ctx.el("error-timeline").hidden, false, "显示 409 错误");
    assert(ctx.el("error-timeline").textContent.includes("stale target"), "错误文案来自后端");
    // 立即重新拉状态，以服务端时间线为准
    const statusCalls = ctx.calls.filter(
      (c) => c.path.includes("/OLD/status") && !c.settled
    );
    assert(statusCalls.length >= 1, "409 后应立即重拉状态");
    ctx.settle("/OLD/status", 200, {
      id: "OLD",
      a: { in_transaction: false, readonly: false, last_read: null, log: [] },
      b: { in_transaction: false, readonly: false, last_read: null, log: [] },
      checkpoint: { last: null, log: [] },
      timeline: [REC1, REC2],
      script: null,
    });
    await flush();
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 2, "以服务端时间线为准");
    assert(texts[1].includes("（补偿 #1）"), "服务端补偿记录渲染");
  });

  // 脚本有效期间：补偿控件禁用，点击不发请求
  await test("脚本有效期间：补偿禁用且点击不发请求", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    // 轮询带回 active 脚本
    ctx.tickTimers();
    await flush();
    ctx.settle("/OLD/status", 200, {
      id: "OLD",
      a: { in_transaction: false, readonly: false, last_read: null, log: [] },
      b: { in_transaction: false, readonly: false, last_read: null, log: [] },
      checkpoint: { last: null, log: [] },
      timeline: [REC1],
      script: {
        id: "S1", status: "active", step: 1, total: 1,
        steps: [{ step: 1, side: "a", op: "begin" }], records: [],
      },
    });
    await flush();
    ctx.settle("/OLD/committed", 200, { committed: 112 });
    await flush();
    assertEq(ctx.el("compensate-btn").disabled, true, "脚本有效时补偿禁用");
    assertEq(ctx.el("compensate-seq").disabled, true, "脚本有效时输入框禁用");
    ctx.el("compensate-seq").value = "1";
    const before = ctx.calls.length;
    ctx.el("compensate-btn").click();
    await flush();
    assertEq(ctx.calls.length, before, "脚本有效期间点击不发请求");
  });

  // 新建进行中禁用；410 冻结后禁用且点击不发请求
  await test("新建进行中禁用；410 冻结后禁用", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("new-exp").click();
    await flush();
    assertEq(ctx.el("compensate-btn").disabled, true, "创建中补偿禁用");
    assertEq(ctx.el("compensate-seq").disabled, true, "创建中输入框禁用");
    ctx.settle("/api/experiments", 500, {}, "POST", { exact: true });
    await flush();
    // 存活页新建失败：恢复旧实验，补偿恢复可用
    assertEq(ctx.el("compensate-btn").disabled, false, "新建失败恢复后补偿可用");

    // 410 冻结
    ctx.settle("/OLD/status", 410, {});
    await flush();
    assertEq(ctx.app().expFrozen, true, "已冻结");
    assertEq(ctx.el("compensate-btn").disabled, true, "冻结时补偿禁用");
    assertEq(ctx.el("compensate-seq").disabled, true, "冻结时输入框禁用");
    ctx.el("compensate-seq").value = "1";
    const before = ctx.calls.length;
    ctx.el("compensate-btn").click();
    await flush();
    assertEq(ctx.calls.length, before, "冻结时点击不发请求");
  });

  // 补偿请求在途时收到 410：冻结
  await test("补偿请求 410：冻结旧实验", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    const call = ctx.findCall("/OLD/compensate", "POST");
    call.settled = true;
    call.resolve({ status: 410, ok: false, json: async () => ({ error: "experiment gone" }) });
    await flush();
    assertEq(ctx.app().expFrozen, true, "410 后冻结");
    assert(ctx.el("deleted-note").textContent.includes("补偿未执行"), "冻结说明");
    assertEq(ctx.el("compensate-btn").disabled, true, "冻结后补偿禁用");
  });

  // 旧实验的迟到补偿响应：不影响新实验时间线
  await test("补偿在途时新建：旧 200 不影响新实验时间线", async () => {
    const ctx = loadApp("?exp=OLD");
    await bootOldLive(ctx);
    ctx.el("compensate-seq").value = "1";
    ctx.el("compensate-btn").click();
    await flush();
    const oldCall = ctx.findCall("/OLD/compensate", "POST");

    ctx.el("new-exp").click();
    await flush();
    await settleNewCreated(ctx);
    assertEq(ctx.app().expId, "NEW", "已切到 NEW");
    assertEq(ctx.el("compensate-seq").value, "", "新实验序号输入已清空");

    oldCall.settled = true;
    oldCall.resolve({
      status: 200, ok: true,
      json: async () => ({ ok: true, error: null, record: REC2, timeline: [REC1, REC2] }),
    });
    await flush();
    assertEq(ctx.app().expId, "NEW", "迟到响应后仍为 NEW");
    const texts = timelineTexts(ctx);
    assertEq(texts.length, 1, "新实验时间线不被旧响应覆盖");
    assert(texts[0].includes("暂无已提交的写入事务"), "新实验时间线为空占位");
    assertEq(ctx.el("error-timeline").hidden, true, "旧响应不显示错误");
  });

  console.log(`\n${passed} 通过，${failed} 失败`);
  process.exit(failed === 0 ? 0 : 1);
}

main();
