from .pa import PA


async def setup(bot):
    await bot.add_cog(PA(bot))
