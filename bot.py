import os
import asyncio
import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Config
TOKEN = os.getenv("DISCORD_TOKEN")
SELLAUTH_KEY = os.getenv("SELLAUTH_API_KEY")
SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")
GUILD_ID = int(os.getenv("GUILD_ID"))
TICKET_CAT_ID = int(os.getenv("TICKET_CATEGORY_ID"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID"))

# Coupon role config — ONLY this role can use /coupon
COUPON_ROLE_ID = 1507791059567120527  # <-- PUT YOUR COUPON ROLE ID HERE

# Stock notifier config (optional - if not set, stock monitoring is disabled)
NOTIFY_GUILD_ID = int(os.getenv("NOTIFY_GUILD_ID", str(GUILD_ID)))
NOTIFY_CHANNEL_ID = int(os.getenv("NOTIFY_CHANNEL_ID", "0")) if os.getenv("NOTIFY_CHANNEL_ID") else 0
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

SELLAUTH_BASE = "https://api.sellauth.com/v1"
SHOP_URL = "https://vortexm4rket.mysellauth.com"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Store previous state to detect changes (stock monitoring)
previous_products = {}  # product_id -> {stock, name, price, etc.}
previous_product_ids = set()

class SellAuthAPI:
    def __init__(self, api_key, shop_id):
        self.api_key = api_key
        self.shop_id = shop_id
        self.headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    async def get_invoice(self, invoice_hash):
        async with aiohttp.ClientSession() as session:
            url = f"{SELLAUTH_BASE}/shops/{self.shop_id}/invoices/{invoice_hash}"
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    return await resp.json()

            url = f"{SELLAUTH_BASE}/shops/{self.shop_id}/invoices"
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    invoices = data.get("data", data if isinstance(data, list) else [])
                    for inv in invoices:
                        if str(inv.get("id")) == invoice_hash:
                            return inv
                        if inv.get("hash") == invoice_hash:
                            return inv
                        if inv.get("custom_id") == invoice_hash:
                            return inv
                        if invoice_hash in str(inv.get("id", "")):
                            return inv
            return None

    async def get_invoice_details(self, invoice_id):
        inv = await self.get_invoice(invoice_id)
        if not inv:
            return None

        # DEBUG: Print full API response to console
        print("=" * 60)
        print("DEBUG: Full API Response:")
        print(str(inv)[:2000])  # Print first 2000 chars
        print("=" * 60)

        # Try to find deliverables
        delivered = []

        if "delivered" in inv and inv["delivered"]:
            delivered = inv["delivered"]
        elif "deliverables" in inv and inv["deliverables"]:
            delivered = inv["deliverables"]
        elif "items" in inv and inv["items"]:
            for item in inv["items"]:
                if isinstance(item, dict):
                    if "delivered" in item and item["delivered"]:
                        delivered.extend(item["delivered"] if isinstance(item["delivered"], list) else [item["delivered"]])
                    elif "value" in item or "data" in item or "account" in item:
                        delivered.append(item)

        # Extract product name - check EVERY possible field
        product_name = None

        # Check product object
        if "product" in inv and isinstance(inv["product"], dict):
            product_name = (inv["product"].get("name") or inv["product"].get("title") 
                          or inv["product"].get("display_name") or inv["product"].get("product_name")
                          or inv["product"].get("label") or inv["product"].get("text"))

        # Check top-level fields
        if not product_name:
            for key in ["product_name", "title", "product_title", "display_name", "name", 
                       "item_name", "product", "item", "description", "label", "text"]:
                if key in inv and inv[key] and isinstance(inv[key], str):
                    product_name = inv[key]
                    break

        # Check items array for product name
        if not product_name and "items" in inv and inv["items"]:
            items = inv["items"]
            if isinstance(items, list) and len(items) > 0:
                first_item = items[0]
                if isinstance(first_item, dict):
                    for key in ["product_name", "name", "title", "display_name", "label", "text", "description"]:
                        if key in first_item and first_item[key] and isinstance(first_item[key], str):
                            product_name = first_item[key]
                            break
                    # Check nested product object in item
                    if not product_name and "product" in first_item and isinstance(first_item["product"], dict):
                        for key in ["name", "title", "display_name", "product_name", "label", "text"]:
                            if key in first_item["product"] and first_item["product"][key]:
                                product_name = first_item["product"][key]
                                break

        # Check product_id and look up
        if not product_name and "product_id" in inv:
            product_name = f"Product #{inv['product_id']}"

        if not product_name:
            product_name = "Unknown Product"

        print(f"DEBUG: Extracted product_name = '{product_name}'")

        # Get total price
        total = 0
        for field in ["total", "price", "amount", "total_price", "grand_total", "cost", "paid", "sum"]:
            if field in inv and inv[field]:
                try:
                    total = float(inv[field])
                    break
                except (ValueError, TypeError):
                    continue

        # Check items for price
        if not total and "items" in inv and inv["items"]:
            for item in inv["items"]:
                if isinstance(item, dict):
                    for field in ["price", "total", "amount", "cost", "paid"]:
                        if field in item and item[field]:
                            try:
                                total += float(item[field])
                            except (ValueError, TypeError):
                                continue

        currency = inv.get("currency", "USD")
        if not currency and "items" in inv and inv["items"]:
            for item in inv["items"]:
                if isinstance(item, dict) and "currency" in item:
                    currency = item["currency"]
                    break

        return {
            "invoice": inv,
            "delivered": delivered,
            "product_name": product_name,
            "total": total,
            "currency": currency,
            "created_at": inv.get("created_at"),
            "completed_at": inv.get("completed_at"),
            "customer": inv.get("customer", {}),
            "status": inv.get("status", "unknown"),
            "raw": inv
        }

    async def get_products(self):
        """Get all products from the shop"""
        async with aiohttp.ClientSession() as session:
            url = f"{SELLAUTH_BASE}/shops/{self.shop_id}/products"
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    products = data.get("data", data if isinstance(data, list) else [])
                    return products
                else:
                    print(f"[ERROR] Failed to fetch products: {resp.status}")
                    return []

    async def create_coupon(self, code, discount, discount_type="fixed", max_uses=1, global_coupon=True):
        async with aiohttp.ClientSession() as session:
            url = f"{SELLAUTH_BASE}/shops/{self.shop_id}/coupons"

            formats_to_try = []
            if discount_type == "fixed":
                formats_to_try.append(str(float(discount)))
                formats_to_try.append(str(int(float(discount) * 100)))
            else:
                formats_to_try.append(str(discount))

            for fmt_idx, discount_value in enumerate(formats_to_try):
                payload = {
                    "code": code,
                    "global": global_coupon,
                    "discount": discount_value,
                    "type": discount_type,
                    "max_uses": max_uses,
                    "uses": 0,
                    "max_uses_per_customer": 1,
                    "disable_if_volume_discount": False
                }

                print(f"[DEBUG] Trying format {fmt_idx+1}: discount={discount_value} (type={type(discount_value).__name__})")
                print(f"[DEBUG] Payload: {payload}")

                async with session.post(url, headers=self.headers, json=payload) as resp:
                    response_text = await resp.text()
                    print(f"[DEBUG] Response: {resp.status} - {response_text[:500]}")

                    if resp.status in [200, 201]:
                        try:
                            data = await resp.json()
                            print(f"[DEBUG] SUCCESS with format {fmt_idx+1}!")
                            return data
                        except:
                            return {"raw_response": response_text}
                    else:
                        print(f"[DEBUG] Format {fmt_idx+1} failed: {resp.status}")

            print(f"[ERROR] All coupon formats failed!")
            return None

sellauth = SellAuthAPI(SELLAUTH_KEY, SHOP_ID)

# ============ STOCK MONITORING HELPERS ============

def extract_product_data(product):
    """Extract all relevant data from a SellAuth product response"""
    if not isinstance(product, dict):
        return None

    product_id = str(product.get("id", product.get("product_id", "")))
    if not product_id:
        return None

    name = None
    for key in ["name", "title", "display_name", "product_name", "label", "text", "description"]:
        if key in product and product[key] and isinstance(product[key], str):
            name = product[key]
            break
    if not name:
        name = "Unknown Product"

    price = None
    for key in ["price", "amount", "cost", "base_price", "total", "price_display", "starting_price"]:
        if key in product and product[key] is not None:
            try:
                price = float(product[key])
                break
            except (ValueError, TypeError):
                continue
    if price is None:
        price = 0.0

    currency = product.get("currency", "USD")
    if not currency:
        currency = "USD"

    stock = None
    for key in ["stock", "quantity", "inventory", "available", "stock_count", "stock_quantity", 
                "items_count", "deliverables_count", "units", "count"]:
        if key in product and product[key] is not None:
            try:
                stock = int(product[key])
                break
            except (ValueError, TypeError):
                continue
    if stock is None:
        stock = 0

    description = product.get("description", "")
    if not description:
        description = product.get("short_description", "")

    image_url = None
    for key in ["image", "thumbnail", "cover_image", "banner", "photo", "picture", "img", "image_url"]:
        if key in product and product[key] and isinstance(product[key], str):
            image_url = product[key]
            break

    variants = []
    if "variants" in product and isinstance(product["variants"], list):
        variants = product["variants"]
    elif "options" in product and isinstance(product["options"], list):
        variants = product["options"]
    elif "items" in product and isinstance(product["items"], list):
        variants = product["items"]

    product_url = None
    slug = None

    for key in ["slug", "url_slug", "permalink", "handle", "seo_slug", "product_slug", "path", "url", "link"]:
        if key in product and product[key] and isinstance(product[key], str):
            val = product[key]
            if val.startswith("http"):
                product_url = val
                break
            elif val.startswith("/"):
                product_url = f"{SHOP_URL}{val}"
                break
            else:
                if not val.isdigit():
                    slug = val
                break

    if not slug and name and name != "Unknown Product":
        import re
        slug = re.sub(r"[^a-zA-Z0-9\s-]", "", name.lower())
        slug = re.sub(r"[\s]+", "-", slug)
        slug = slug.strip('-')

    if slug:
        product_url = f"{SHOP_URL}/product/{slug}"
    else:
        product_url = f"{SHOP_URL}/product/{product_id}"

    if not product_url:
        product_url = f"{SHOP_URL}/product/{product_id}"
        print(f"[DEBUG] Fallback URL used: {product_url}")

    return {
        "id": product_id,
        "name": name,
        "price": price,
        "currency": currency,
        "stock": stock,
        "description": description,
        "image_url": image_url,
        "variants": variants,
        "product_url": product_url,
        "raw": product
    }

# ============ STOCK NOTIFICATION EMBEDS ============

def create_new_product_embed(data):
    name = data["name"]
    price = data["price"]
    currency = data["currency"]
    stock = data["stock"]
    image_url = data["image_url"]
    product_url = data["product_url"]
    variants = data["variants"]

    embed = discord.Embed(
        title=f"{name} Added",
        description=f"Our product **{name}** has just been added!\n[Buy Now]({product_url})",
        color=0x5865F2,
        timestamp=datetime.now()
    )

    if variants and len(variants) > 0:
        variant_lines = []
        for v in variants[:5]:
            if isinstance(v, dict):
                v_name = v.get("name") or v.get("title") or v.get("label") or name
                v_price = v.get("price", price)
                v_stock = v.get("stock", stock)
                variant_lines.append(f"📦 {v_name} | 💰 {v_price} {currency} | 📦 {v_stock} in stock")
            else:
                variant_lines.append(str(v))
        if variant_lines:
            embed.add_field(name="📦 Variant | 💰 Price | 📦 Stock", value="\n".join(variant_lines), inline=False)
    else:
        embed.add_field(name="📦 Variant", value=name, inline=True)
        embed.add_field(name="💰 Price", value=f"{price} {currency}", inline=True)
        embed.add_field(name="📦 Stock", value=f"{stock}", inline=True)

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text="VortexMarket")
    return embed

def create_restock_embed(data, old_stock, new_stock):
    name = data["name"]
    price = data["price"]
    currency = data["currency"]
    stock = data["stock"]
    image_url = data["image_url"]
    product_url = data["product_url"]
    variants = data["variants"]

    embed = discord.Embed(
        title=f"{name} Restocked",
        description=f"Our product **{name}** has just been restocked!\n[Buy Now]({product_url})",
        color=0x57F287,
        timestamp=datetime.now()
    )

    if variants and len(variants) > 0:
        variant_lines = []
        for v in variants[:5]:
            if isinstance(v, dict):
                v_name = v.get("name") or v.get("title") or v.get("label") or name
                v_price = v.get("price", price)
                v_stock = v.get("stock", stock)
                variant_lines.append(f"📦 {v_name} | 💰 {v_price} {currency} | 📦 {v_stock} in stock")
            else:
                variant_lines.append(str(v))
        if variant_lines:
            embed.add_field(name="📦 Variant | 💰 Price | 📦 Stock", value="\n".join(variant_lines), inline=False)
    else:
        embed.add_field(name="📦 Variant", value=name, inline=True)
        embed.add_field(name="💰 Price", value=f"{price} {currency}", inline=True)
        embed.add_field(name="📦 Stock", value=f"{stock}", inline=True)

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text="VortexMarket")
    return embed

def create_out_of_stock_embed(data):
    name = data["name"]
    price = data["price"]
    currency = data["currency"]
    image_url = data["image_url"]

    embed = discord.Embed(
        title=f"{name} Out of Stock",
        description=f"**{name}** is now out of stock!",
        color=0xED4245,
        timestamp=datetime.now()
    )

    embed.add_field(name="💰 Price", value=f"{price} {currency}", inline=True)
    embed.add_field(name="📦 Stock", value="**0**", inline=True)

    if image_url:
        embed.set_thumbnail(url=image_url)

    embed.set_footer(text="VortexMarket")
    return embed

def create_low_stock_embed(data):
    name = data["name"]
    price = data["price"]
    currency = data["currency"]
    stock = data["stock"]
    image_url = data["image_url"]

    embed = discord.Embed(
        title=f"⚠️ {name} Low Stock",
        description=f"**{name}** is running low!",
        color=0xFEE75C,
        timestamp=datetime.now()
    )

    embed.add_field(name="💰 Price", value=f"{price} {currency}", inline=True)
    embed.add_field(name="📦 Stock Remaining", value=f"**{stock}** left", inline=True)

    if image_url:
        embed.set_thumbnail(url=image_url)

    embed.set_footer(text="VortexMarket")
    return embed

# ============ TICKET SYSTEM ============

class TicketTypeSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Support", description="General support questions", emoji="💬", value="support"),
            discord.SelectOption(label="Replacement", description="Request a replacement for a product", emoji="🔄", value="replacement"),
            discord.SelectOption(label="Not Received", description="Did not receive your product", emoji="📦", value="not_received")
        ]
        super().__init__(placeholder="Select ticket type", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        ticket_type = self.values[0]
        if ticket_type == "support":
            await interaction.response.send_modal(SupportModal())
        elif ticket_type == "replacement":
            await interaction.response.send_modal(ReplacementInvoiceModal())
        elif ticket_type == "not_received":
            await interaction.response.send_modal(NotReceivedModal())

class TicketTypeView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())

# ============ MODALS ============

class SupportModal(ui.Modal, title="Support Ticket"):
    issue = ui.TextInput(label="Describe your issue", style=discord.TextStyle.paragraph, 
                         placeholder="Explain your problem in detail...", required=True, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        await create_standard_ticket(interaction, "support", {"issue": self.issue.value})

class ReplacementInvoiceModal(ui.Modal, title="Replacement Request"):
    invoice_id = ui.TextInput(label="Invoice ID (Do not include the #)", 
                              placeholder="e.g., 5f88ff3759bb2-0000012994659", required=True, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        invoice_data = await sellauth.get_invoice_details(self.invoice_id.value.strip())

        if not invoice_data or not invoice_data["invoice"]:
            await interaction.followup.send(
                "❌ **Order not found.** Please check your Invoice ID and try again.\n"
                "Make sure you're using the ID from your purchase receipt.", ephemeral=True)
            return

        delivered = invoice_data.get("delivered", [])

        if not delivered:
            await create_replacement_ticket(interaction, invoice_data, [], invoice_hash=self.invoice_id.value.strip(), no_deliverables=True)
            return

        view = DeliverableSelectView(delivered, invoice_data, self.invoice_id.value.strip())
        embed = discord.Embed(title="Select Deliverable to Replace", 
                              description="Choose which account/item you want to replace.", color=0x5865F2)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class DeliverableSelect(ui.Select):
    def __init__(self, deliverables, invoice_data, invoice_hash=""):
        self.invoice_data = invoice_data
        self.invoice_hash = invoice_hash
        options = []
        for i, item in enumerate(deliverables[:25]):
            if isinstance(item, dict):
                display = (item.get("value") or item.get("data") or item.get("account") 
                          or item.get("email") or item.get("username") or str(item))
            else:
                display = str(item)
            label = display[:50] if len(display) > 50 else display
            if not label or label == "None" or len(label) < 3:
                label = f"Item {i+1}"
            options.append(discord.SelectOption(label=label[:100], value=str(i), description="Click to replace this"))

        super().__init__(placeholder="Select the deliverable to replace", 
                         min_values=1, max_values=min(len(options), len(deliverables)), options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        selected = []
        for val in self.values:
            idx = int(val)
            delivered = self.invoice_data.get("delivered", [])
            if idx < len(delivered):
                selected.append(delivered[idx])
        await create_replacement_ticket(interaction, self.invoice_data, selected, invoice_hash=self.invoice_hash)

class DeliverableSelectView(ui.View):
    def __init__(self, deliverables, invoice_data, invoice_hash=""):
        super().__init__(timeout=300)
        self.add_item(DeliverableSelect(deliverables, invoice_data, invoice_hash))

class NotReceivedModal(ui.Modal, title="Not Received"):
    order_id = ui.TextInput(label="Order ID (Do not include the #)", placeholder="Your order ID...", required=True, max_length=100)
    explanation = ui.TextInput(label="Explain issue", style=discord.TextStyle.paragraph, 
                               placeholder="Describe what you expected to receive...", required=True, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        await create_standard_ticket(interaction, "not-received", {
            "order_id": self.order_id.value, "explanation": self.explanation.value})

# ============ TICKET CREATION ============

async def create_standard_ticket(interaction: discord.Interaction, ticket_type: str, data: dict):
    guild = interaction.guild
    user = interaction.user
    channel_name = f"{user.name}-{ticket_type}"

    category = guild.get_channel(TICKET_CAT_ID)
    staff_role = guild.get_role(STAFF_ROLE_ID)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True, manage_channels=True)

    channel = await guild.create_text_channel(name=channel_name[:100], category=category, 
                                               overwrites=overwrites, reason=f"Ticket created by {user}")

    embed = discord.Embed(title=f"{ticket_type.replace('-', ' ').title()} Ticket", 
                          color=0x5865F2, timestamp=datetime.now())
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

    if ticket_type == "support":
        embed.description = data["issue"]
        embed.add_field(name="Type", value="💬 Support", inline=True)
    elif ticket_type == "not-received":
        embed.add_field(name="Order ID", value="`[Verified]`", inline=True)
        embed.add_field(name="Issue", value=data["explanation"], inline=False)
        embed.add_field(name="Type", value="📦 Not Received", inline=True)

    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Status", value="⏳ Open", inline=True)
    embed.set_footer(text=f"VortexMarket")

    view = StaffActionView(channel.id)
    msg = await channel.send(content=f"{user.mention} | {staff_role.mention if staff_role else '@staff'}",
                              embed=embed, view=view)

    await interaction.response.send_message(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)

async def create_replacement_ticket(interaction: discord.Interaction, invoice_data: dict, 
                                      selected_deliverables: list, invoice_hash: str = "", no_deliverables=False):
    print(f"[DEBUG] create_replacement_ticket called! invoice_hash='{invoice_hash}'")
    guild = interaction.guild
    user = interaction.user
    channel_name = f"{user.name}-replacement"

    category = guild.get_channel(TICKET_CAT_ID)
    staff_role = guild.get_role(STAFF_ROLE_ID)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True, manage_channels=True)

    channel = await guild.create_text_channel(name=channel_name[:100], category=category,
                                               overwrites=overwrites, reason=f"Replacement ticket by {user}")

    print(f"[DEBUG] Storage: invoice_hash param='{invoice_hash}'")
    stored_invoice = invoice_hash
    if not stored_invoice and invoice_data and "invoice" in invoice_data:
        inv = invoice_data["invoice"]
        if isinstance(inv, dict):
            stored_invoice = inv.get("unique_id", "") or str(inv.get("id", ""))

    print(f"[DEBUG] Storing invoice for channel {channel.id}: '{stored_invoice}'")
    if stored_invoice and stored_invoice != "None":
        channel_invoices[channel.id] = stored_invoice
        print(f"[DEBUG] Stored! channel_invoices = {channel_invoices}")
    else:
        print(f"[DEBUG] WARNING: No invoice hash to store!")

    completed_at = invoice_data.get("completed_at")
    if completed_at:
        try:
            dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%A, %d. %B %Y um %H:%M")
        except:
            date_str = completed_at
    else:
        date_str = "Unknown"

    product_name = invoice_data.get("product_name", "Unknown Product")

    account_details = []
    if no_deliverables:
        account_details.append("*No deliverables found - staff will provide replacement*")
    else:
        for item in selected_deliverables:
            if isinstance(item, dict):
                val = (item.get("value") or item.get("data") or item.get("account")
                       or item.get("email") or item.get("username") or str(item))
            else:
                val = str(item)
            account_details.append(val)

    account_text = "\n".join(account_details) if account_details else "*Details hidden*"
    price = f"{invoice_data['total']} {invoice_data['currency']}"
    lang = "🇬🇧 English"

    embed = discord.Embed(title="Replacement", color=0x2b2d31, timestamp=datetime.now())
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Language", value=lang, inline=True)
    embed.add_field(name="Purchase date", value=date_str, inline=True)

    embed.add_field(name="Product", value=f"{product_name} · {price} · 1 account", inline=False)

    embed.add_field(name="​", value=f"```{account_text[:1000]}```", inline=False)

    time_str = datetime.now().strftime("%H:%M")
    staff_line = f"⚡ {staff_role.mention if staff_role else '@staff'} | 8.0 · heute um {time_str} Uhr"
    embed.add_field(name="​", value=staff_line, inline=False)

    embed.set_footer(text=f"VortexMarket")

    view = StaffActionView(channel.id)
    msg = await channel.send(content=f"{user.mention} | {staff_role.mention if staff_role else '@staff'}",
                              embed=embed, view=view)

    await interaction.followup.send(f"✅ Replacement ticket created: {channel.mention}", ephemeral=True)

# ============ STAFF ACTION BUTTONS WITH CLAIM/UNCLAIM TOGGLE ============

class StaffActionView(ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.claimed_by = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role and staff_role in interaction.user.roles:
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message("❌ Only staff can use these buttons.", ephemeral=True)
        return False

    @ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🟢", custom_id="claim_btn")
    async def claim(self, interaction: discord.Interaction, button: ui.Button):
        embed = interaction.message.embeds[0] if interaction.message.embeds else None

        if self.claimed_by is None:
            self.claimed_by = interaction.user.id
            button.label = "Unclaim"
            button.style = discord.ButtonStyle.secondary
            button.emoji = "⚪"

            if embed:
                for i, field in enumerate(embed.fields):
                    if field.name == "Status":
                        embed.set_field_at(i, name="Status", value=f"🟢 Claimed by {interaction.user.mention}", inline=True)
                        break
                await interaction.message.edit(embed=embed, view=self)
            await interaction.response.send_message(f"✅ Ticket claimed by {interaction.user.mention}", ephemeral=True)

        elif self.claimed_by == interaction.user.id:
            self.claimed_by = None
            button.label = "Claim"
            button.style = discord.ButtonStyle.success
            button.emoji = "🟢"

            if embed:
                for i, field in enumerate(embed.fields):
                    if field.name == "Status":
                        embed.set_field_at(i, name="Status", value="⏳ Open", inline=True)
                        break
                await interaction.message.edit(embed=embed, view=self)
            await interaction.response.send_message(f"⚪ Ticket unclaimed by {interaction.user.mention}", ephemeral=True)

        else:
            await interaction.response.send_message("❌ This ticket is already claimed by someone else.", ephemeral=True)

    @ui.button(label="Hold", style=discord.ButtonStyle.primary, emoji="⏸️", custom_id="hold_btn")
    async def hold(self, interaction: discord.Interaction, button: ui.Button):
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            for i, field in enumerate(embed.fields):
                if field.name == "Status":
                    embed.set_field_at(i, name="Status", value=f"⏸️ On Hold by {interaction.user.mention}", inline=True)
                    break
            await interaction.message.edit(embed=embed)
        await interaction.response.send_message(f"⏸️ Ticket put on hold by {interaction.user.mention}", ephemeral=True)

    @ui.button(label="Finish", style=discord.ButtonStyle.success, emoji="✅", custom_id="finish_btn")
    async def finish(self, interaction: discord.Interaction, button: ui.Button):
        channel = interaction.guild.get_channel(self.channel_id)
        if channel:
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed:
                for i, field in enumerate(embed.fields):
                    if field.name == "Status":
                        embed.set_field_at(i, name="Status", value=f"✅ Finished by {interaction.user.mention}", inline=True)
                        break
                await interaction.message.edit(embed=embed)

            await interaction.response.send_message("✅ Finishing and closing ticket in 5 seconds...", ephemeral=True)
            await asyncio.sleep(5)
            await channel.delete(reason=f"Finished by {interaction.user}")

# ============ COUPON HELPERS ============

def generate_coupon_code(length=12):
    prefix = "REPLACE"
    digits = ''.join(random.choices(string.digits, k=length - len(prefix) - 1))
    return f"{prefix}-{digits}"

async def send_coupon_dm(user, coupon_code, amount, discount_type="fixed"):
    try:
        dm_channel = await user.create_dm()

        embed = discord.Embed(
            title="Your Replacement Coupon - VortexMarket",
            description="Your replacement request has been approved. Here is your coupon code to use at checkout.",
            color=0x57F287,
            timestamp=datetime.now()
        )

        embed.add_field(name="Coupon value", value=f"{amount} EUR", inline=False)
        embed.add_field(name="Coupon code", value=f"```{coupon_code}```", inline=False)

        instructions = "1. Visit [VortexMarket](https://vortexm4rket.mysellauth.com)\n2. Add your products to the cart\n3. Apply the coupon code at checkout"
        embed.add_field(name="How to use it", value=instructions, inline=False)

        embed.set_footer(text="VortexMarket - Valid for 30 days - Single use only")

        await dm_channel.send(embed=embed)

        warning_embed = discord.Embed(
            title="Single Use Warning",
            description="This coupon is single use only. Once redeemed, it cannot be used again.\n\nUse it wisely!",
            color=0xFEE75C,
            timestamp=datetime.now()
        )
        warning_embed.set_footer(text="VortexMarket Coupon System")
        await dm_channel.send(embed=warning_embed)

        await dm_channel.send(
            f"**Copy Coupon Code**\n`{coupon_code}`\n\n"
            f"[Go to VortexMarket](https://vortexm4rket.mysellauth.com)"
        )

        return True
    except discord.Forbidden:
        print(f"[ERROR] Cannot DM user {user.id} - DMs disabled")
        return False
    except Exception as e:
        print(f"[ERROR] Failed to send DM: {e}")
        return False

# ============ STOCK MONITORING TASK ============

@tasks.loop(seconds=POLL_INTERVAL)
async def monitor_stock():
    global previous_products, previous_product_ids

    if not NOTIFY_CHANNEL_ID:
        print("[STOCK] NOTIFY_CHANNEL_ID not set, skipping stock check")
        return

    try:
        guild = bot.get_guild(NOTIFY_GUILD_ID)
        if not guild:
            print(f"[STOCK] Guild {NOTIFY_GUILD_ID} not found")
            return

        channel = guild.get_channel(NOTIFY_CHANNEL_ID)
        if not channel:
            print(f"[STOCK] Channel {NOTIFY_CHANNEL_ID} not found in guild {guild.name}")
            return

        print(f"[STOCK] Checking stock... (previous: {len(previous_product_ids)} products)")
        products = await sellauth.get_products()

        if not products:
            print("[STOCK] No products returned from API")
            return

        print(f"[STOCK] Fetched {len(products)} products from API")

        current_product_ids = set()
        current_products = {}

        for product in products:
            data = extract_product_data(product)
            if not data:
                print(f"[STOCK] Failed to extract data for product: {product.get('id', 'unknown')}")
                continue
            pid = data["id"]
            current_product_ids.add(pid)
            current_products[pid] = data
            print(f"[STOCK] Product: {data['name']} | Stock: {data['stock']} | Price: {data['price']} {data['currency']}")

        if not previous_product_ids:
            previous_products = current_products.copy()
            previous_product_ids = current_product_ids.copy()
            print(f"[INFO] Stock baseline set: {len(current_products)} products")
            return

        new_products = current_product_ids - previous_product_ids
        for pid in new_products:
            data = current_products[pid]
            embed = create_new_product_embed(data)
            await channel.send(embed=embed)
            print(f"[NOTIFY] New product: {data['name']}")

        for pid in current_product_ids & previous_product_ids:
            old_data = previous_products[pid]
            new_data = current_products[pid]

            old_stock = old_data["stock"]
            new_stock = new_data["stock"]

            if old_stock != new_stock:
                print(f"[STOCK] Change detected for {new_data['name']}: {old_stock} -> {new_stock}")
                if new_stock > 0 and old_stock == 0:
                    embed = create_restock_embed(new_data, old_stock, new_stock)
                    await channel.send(embed=embed)
                    print(f"[NOTIFY] Restocked: {new_data['name']} ({old_stock} -> {new_stock})")
                elif new_stock > old_stock and old_stock > 0:
                    embed = create_restock_embed(new_data, old_stock, new_stock)
                    await channel.send(embed=embed)
                    print(f"[NOTIFY] Restocked: {new_data['name']} ({old_stock} -> {new_stock})")
                elif new_stock == 0 and old_stock > 0:
                    embed = create_out_of_stock_embed(new_data)
                    await channel.send(embed=embed)
                    print(f"[NOTIFY] Out of stock: {new_data['name']}")
                elif 0 < new_stock <= 5 and old_stock > 5:
                    embed = create_low_stock_embed(new_data)
                    await channel.send(embed=embed)
                    print(f"[NOTIFY] Low stock: {new_data['name']} ({new_stock} left)")

        previous_products = current_products.copy()
        previous_product_ids = current_product_ids.copy()
        print(f"[STOCK] Check complete. Tracked {len(current_products)} products.")

    except Exception as e:
        print(f"[ERROR] Stock monitoring error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

@monitor_stock.error
async def monitor_stock_error(error):
    print(f"[ERROR] monitor_stock task crashed: {type(error).__name__}: {error}")
    import traceback
    traceback.print_exc()

# ============ COMMANDS ============

@bot.event
async def on_ready():
    print(f"✅ Bot logged in as {bot.user}")
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print("✅ Slash commands synced")

    if NOTIFY_CHANNEL_ID and not monitor_stock.is_running():
        monitor_stock.start()
        print(f"🔍 Stock monitoring started (channel: {NOTIFY_CHANNEL_ID}, interval: {POLL_INTERVAL}s)")

@bot.event
async def on_disconnect():
    if monitor_stock.is_running():
        monitor_stock.stop()

@bot.tree.command(name="ticket", description="Open a support ticket", guild=discord.Object(id=GUILD_ID))
async def ticket_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="VortexMarket Support",
        description="If you need help, click on the option corresponding to the **type of ticket** you want to open.\n\n"
                    "Response time may vary due to many factors, so please be patient.",
        color=0x1a1a1a
    )
    embed.set_author(name="VortexMarket Assistant", icon_url=bot.user.display_avatar.url)
    embed.set_footer(text="VortexMarket Ticket System")
    view = TicketTypeView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="setup", description="Setup the ticket panel (Admin only)", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def setup_command(interaction: discord.Interaction, channel: discord.TextChannel):
    embed = discord.Embed(
        title="VortexMarket Support",
        description="If you need help, click on the option corresponding to the **type of ticket** you want to open.\n\n"
                    "Response time may vary due to many factors, so please be patient.",
        color=0x1a1a1a
    )
    embed.set_author(name="VortexMarket Assistant", icon_url=bot.user.display_avatar.url)
    embed.set_footer(text="VortexMarket Ticket System")
    view = TicketTypeView()
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Ticket panel sent to {channel.mention}", ephemeral=True)

# Store invoice IDs per channel (set when replacement ticket is created)
channel_invoices = {}  # channel_id -> invoice_id

# Store active coupons
coupons = {}  # coupon_code -> {amount, user_id, created_at, used}

@bot.tree.command(name="invoice", description="Show invoice checkout link for this ticket (Staff only)", guild=discord.Object(id=GUILD_ID))
async def invoice_command(interaction: discord.Interaction):
    staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
    if staff_role and staff_role not in interaction.user.roles:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only staff can use this command.", ephemeral=True)
            return

    channel_id = interaction.channel_id
    print(f"[DEBUG] /invoice called in channel {channel_id}")
    print(f"[DEBUG] channel_invoices contents: {channel_invoices}")
    invoice_id = channel_invoices.get(channel_id)

    if not invoice_id:
        await interaction.response.send_message(
            "❌ No invoice found for this ticket. This command only works in replacement tickets.", 
            ephemeral=True
        )
        return

    checkout_url = f"https://vortexm4rket.mysellauth.com/checkout/{invoice_id}"

    embed = discord.Embed(
        title="📋 Invoice Checkout Link",
        description=f"Click the link below to view the invoice checkout page.",
        color=0x5865F2,
        timestamp=datetime.now()
    )

    embed.add_field(name="Invoice ID", value=f"`{invoice_id}`", inline=True)
    embed.add_field(name="🔗 Checkout Link", value=f"[Click here to view invoice]({checkout_url})", inline=False)
    embed.add_field(name="Direct URL", value=f"`{checkout_url}`", inline=False)

    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="VortexMarket Invoice System")

    await interaction.response.send_message(embed=embed, ephemeral=True)


def has_coupon_role(interaction: discord.Interaction) -> bool:
    """Check if user has the EXACT coupon role. Admins and other high roles are BLOCKED unless they have this role."""
    user = interaction.user
    guild = interaction.guild

    # NO bypass for administrators, moderators, or anyone else
    # ONLY the specific COUPON_ROLE_ID grants access

    if COUPON_ROLE_ID:
        coupon_role = guild.get_role(COUPON_ROLE_ID)
        if coupon_role:
            has_role = coupon_role in user.roles
            print(f"[COUPON CHECK] User {user} has role '{coupon_role.name}': {has_role}")
            return has_role
        else:
            print(f"[COUPON CHECK] ERROR: Coupon role with ID {COUPON_ROLE_ID} not found in guild!")
            return False
    else:
        print(f"[COUPON CHECK] ERROR: COUPON_ROLE_ID is not set!")
        return False


@bot.tree.command(name="coupon", description="Generate a replacement coupon for a user (Coupon role only)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    user="The user to send the coupon to",
    amount="Coupon value in EUR (e.g., 0.75)",
    prefix="Coupon prefix (default: REPLACE)"
)
async def coupon_command(interaction: discord.Interaction, user: discord.User, amount: float, prefix: str = "REPLACE"):
    if not has_coupon_role(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command. Only users with the Coupon role can generate coupons.", 
            ephemeral=True
        )
        return

    import random
    import string
    from datetime import datetime, timedelta

    random_digits = ''.join(random.choices(string.digits, k=12))
    coupon_code = f"{prefix}-{random_digits}"

    print(f"[DEBUG] Creating coupon: {coupon_code} for amount: {amount}")

    coupon_data = await sellauth.create_coupon(
        code=coupon_code,
        discount=amount,
        discount_type="fixed",
        max_uses=1,
        global_coupon=True
    )

    if not coupon_data:
        await interaction.response.send_message(
            "❌ Failed to create coupon on SellAuth API. Check console for errors.",
            ephemeral=True
        )
        return

    print(f"[DEBUG] Coupon created successfully: {coupon_data}")

    coupons[coupon_code] = {
        "amount": amount,
        "user_id": user.id,
        "created_at": datetime.now(),
        "used": False,
        "created_by": interaction.user.id,
        "sellauth_id": coupon_data.get("id", "unknown")
    }

    embed = discord.Embed(
        title="🎫 Your Replacement Coupon — VortexMarket",
        description="Your replacement request has been approved. Here is your coupon code to use at checkout.",
        color=0x57F287,
        timestamp=datetime.now()
    )

    embed.add_field(name="🎫 Coupon value", value=f"€{amount:.2f}", inline=False)
    embed.add_field(name="🎟️ Coupon code", value=f"`{coupon_code}`", inline=False)

    how_to = (
        "1. Visit [VortexMarket](https://vortexm4rket.mysellauth.com)\n"
        "2. Add your products to the cart\n"
        "3. Apply the coupon code at checkout"
    )
    embed.add_field(name="📋 How to use it", value=how_to, inline=False)

    valid_until = datetime.now() + timedelta(days=30)
    embed.add_field(
        name="ℹ️ Info",
        value=f"VortexMarket • Valid until {valid_until.strftime('%d %b %Y')} • Single use only",
        inline=False
    )

    warning_embed = discord.Embed(
        title="⚠️ Single Use Warning",
        description="This coupon is **single use only**. Once redeemed, it cannot be used again.\nUse it wisely!",
        color=0xFEE75C,
        timestamp=datetime.now()
    )

    try:
        await user.send(embeds=[embed, warning_embed])

        await interaction.response.send_message(
            f"✅ Coupon `€{amount:.2f}` sent to {user.mention}!\nCode: `{coupon_code}`",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Could not DM {user.mention}. They may have DMs disabled.\n"
            f"Coupon code: `{coupon_code}`",
            ephemeral=True
        )



@bot.tree.command(name="stockstatus", description="Show current stock status", guild=discord.Object(id=GUILD_ID))
async def stock_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    products = await sellauth.get_products()
    if not products:
        await interaction.followup.send("❌ Could not fetch products.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📦 Current Stock Status",
        color=0x5865F2,
        timestamp=datetime.now()
    )

    count = 0
    for product in products:
        data = extract_product_data(product)
        if not data:
            continue

        if count >= 25:
            break

        stock = data["stock"]
        status_emoji = "🟢" if stock > 10 else "🟡" if stock > 0 else "🔴"
        embed.add_field(
            name=f"{status_emoji} {data['name'][:50]}",
            value=f"📦 Stock: **{stock}** | 💰 Price: {data['price']} {data['currency']}",
            inline=True
        )
        count += 1

    embed.set_footer(text=f"VortexMarket | {len(products)} products total")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="forcerun", description="Force a stock check now (Admin)", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
async def force_run(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 Running stock check now...", ephemeral=True)
    monitor_stock.restart()
    await interaction.followup.send("✅ Stock check completed.", ephemeral=True)

@bot.tree.command(name="setinterval", description="Set polling interval in seconds (Admin)", guild=discord.Object(id=GUILD_ID))
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(seconds="Polling interval in seconds (min 10)")
async def set_interval(interaction: discord.Interaction, seconds: int):
    global POLL_INTERVAL
    if seconds < 10:
        await interaction.response.send_message("❌ Minimum interval is 10 seconds.", ephemeral=True)
        return

    POLL_INTERVAL = seconds
    monitor_stock.change_interval(seconds=seconds)
    await interaction.response.send_message(f"✅ Polling interval set to {seconds} seconds.", ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ An error occurred: {str(error)}", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
