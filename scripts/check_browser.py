import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        try:
            page = await browser.new_page()
            await page.set_content(
                "<html><head><title>FlipTech Browser Check</title></head>"
                "<body>Chromium is working</body></html>"
            )

            print("Chromium:", browser.version)
            print("Заголовок:", await page.title())
            print("Браузер успешно запущен!")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
