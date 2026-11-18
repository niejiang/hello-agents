import asyncio

async def demo_async():
    print("开始执行")
    return "完成"


def main():
    # 只创建协程对象，还不会打印“开始执行”
    coro = demo_async()

    # 开始驱动协程执行，最终得到返回值“完成”
    result = asyncio.run(coro)
    result1 = asyncio.run(coro)
    print(result)
    print(result1)


if __name__ == "__main__":
    main()