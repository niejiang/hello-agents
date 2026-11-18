"""在 PyCharm 中对照调试协程、线程和阻塞等待。

直接 Run / Debug 此文件即可，只使用 Python 标准库，不发送网络请求。

修改 DEMO_TO_RUN 可单独观察一个场景：
    "async"    同一线程中的多个协程；等待时事件循环心跳继续。
    "thread"   线程池执行任务；主线程通过 Future.result() 阻塞等待。
    "blocking" async def 内调用 time.sleep()；事件循环心跳一起暂停。
    "all"      依次运行三个场景。

搜索 BP-A、BP-T、BP-B 可找到建议设置断点的可执行语句。
观察耗时先用 Run；断点停留也会计入耗时，暂停所有线程会让日志一起停止。
观察协程切换时，可在多个任务内设置断点，再用 Resume 继续运行。
"""

import asyncio
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


DEMO_TO_RUN = "all"
WAIT_SECONDS = 2.0
HEARTBEAT_INTERVAL = 0.4

PROGRAM_STARTED = time.perf_counter()
LOGGER = logging.getLogger("async_await_debug")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [线程=%(threadName)s ID=%(thread)d] %(message)s",
        datefmt="%H:%M:%S",
    )
)
LOGGER.addHandler(console_handler)


def log(message: str) -> None:
    """线程编号由 logging 自动记录；另外记录当前协程任务名。"""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        # 普通主线程代码、线程池代码没有正在运行的 asyncio 事件循环。
        task = None
    task_name = task.get_name() if task is not None else "无 asyncio 任务"
    elapsed = time.perf_counter() - PROGRAM_STARTED
    LOGGER.info("[+%5.2fs] [任务=%s] %s", elapsed, task_name, message)


async def heartbeat(stop: asyncio.Event) -> None:
    """这条心跳本身也是协程，和异步请求共用事件循环线程。"""
    count = 0
    while not stop.is_set():
        count += 1
        log(f"心跳 {count}：事件循环能够执行其他任务")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def async_request(name: str, delay: float) -> str:
    """用异步计时器模拟等待模型响应，不会创建工作线程。"""
    request_id = f"请求-{name}"  # 下断点后可观察这个局部变量仍被保留。
    log(f"[BP-A1] {request_id} 开始，准备异步等待 {delay:.1f} 秒")
    await asyncio.sleep(delay)  # BP-A2：等待未完成时，当前任务会暂停。
    result = f"{request_id} 的响应"
    log(f"[BP-A3] {request_id} 恢复，仍在相同线程，result={result}")
    return result


async def demo_async() -> None:
    """场景一：任务名不同，线程 ID 相同，等待期间仍有心跳。"""
    asyncio.current_task().set_name("异步场景-主任务")
    log("========== 场景一：协程暂停，事件循环继续 ==========")
    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat(stop), name="异步场景-心跳")

    # 创建协程对象没有新建线程；create_task 将它们交给事件循环调度。
    task_a = asyncio.create_task(async_request("A", WAIT_SECONDS), name="协程-A")
    task_b = asyncio.create_task(async_request("B", WAIT_SECONDS / 2), name="协程-B")
    try:
        log("[BP-A4] 主任务等待 A、B，但心跳和 A、B 仍可以交替执行")
        results = await asyncio.gather(task_a, task_b)  # BP-A5
        log(f"[BP-A6] 主任务恢复，收集到结果：{results}")
    finally:
        stop.set()
        await heartbeat_task
    log("观察：主任务、A、B、心跳的线程 ID 全部相同，任务名不同。")


def blocking_request(name: str, delay: float) -> str:
    """在线程池的工作线程中，用阻塞计时器模拟耗时调用。"""
    request_id = f"请求-{name}"
    log(f"[BP-T1] {request_id} 开始，这是线程池工作线程")
    time.sleep(delay)  # BP-T2：阻塞当前工作线程，其他工作线程仍可运行。
    result = f"{request_id} 的响应"
    log(f"[BP-T3] {request_id} 完成，即将返回给 Future")
    return result


def demo_thread() -> None:
    """场景二：工作线程与主线程不同，result() 阻塞调用它的线程。"""
    log("========== 场景二：工作线程执行，主线程阻塞等待 ==========")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="工作线程") as pool:
        future_a = pool.submit(blocking_request, "A", WAIT_SECONDS)
        future_b = pool.submit(blocking_request, "B", WAIT_SECONDS / 2)

        log("[BP-T4] 主线程即将调用 future_a.result()；A 未完成就阻塞在这里")
        result_a = future_a.result()  # BP-T5：相当于 Java Future.get() 的等待方式。
        log(f"[BP-T6] A 已完成，主线程才继续执行这一行：{result_a}")

        result_b = future_b.result()
        log(f"收集 B 的结果：{result_b}")
    log("观察：B 可以先完成，但主线程仍要等正在等待的 A。")


async def blocking_async_request() -> str:
    """故意写错：async def 不会把里面的普通阻塞函数变成异步操作。"""
    log("[BP-B1] 进入 async 方法，接下来直接调用 time.sleep()")
    time.sleep(WAIT_SECONDS)  # BP-B2：阻塞事件循环线程，心跳也无法执行！
    log("[BP-B3] time.sleep() 结束；这段时间事件循环心跳出现了空档")
    return "阻塞调用的结果"


async def demo_blocking() -> None:
    """场景三：对协程使用 await，仍无法消除其内部的同步阻塞。"""
    asyncio.current_task().set_name("错误异步场景-主任务")
    log("========== 场景三：async 方法内部阻塞事件循环 ==========")
    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat(stop), name="错误异步场景-心跳")
    try:
        await asyncio.sleep(HEARTBEAT_INTERVAL * 2)  # 先让心跳正常输出几次。
        log("[BP-B4] 虽然下一行有 await，方法内部仍可能阻塞线程")
        result = await blocking_async_request()  # BP-B5
        log(f"主任务拿到结果：{result}")
        await asyncio.sleep(HEARTBEAT_INTERVAL * 2)  # 让心跳恢复，便于对照。
    finally:
        stop.set()
        await heartbeat_task
    log("观察：所有日志仍是同一线程，但 time.sleep() 期间心跳停止。")


def main() -> None:
    modes = {"all", "async", "thread", "blocking"}
    if DEMO_TO_RUN not in modes:
        raise ValueError(f"DEMO_TO_RUN 必须是 {sorted(modes)} 中的一个")
    log(f"启动调试示例，DEMO_TO_RUN={DEMO_TO_RUN}")
    log(f"主线程 ID={threading.get_ident()}；没有网络请求，也不会调用模型 API。")
    if DEMO_TO_RUN in {"all", "async"}:
        asyncio.run(demo_async())
    if DEMO_TO_RUN in {"all", "thread"}:
        demo_thread()
    if DEMO_TO_RUN in {"all", "blocking"}:
        asyncio.run(demo_blocking())
    log("演示结束。建议分别用 Run 观察时间，用 Debug 观察变量和调用位置。")


if __name__ == "__main__":
    main()
