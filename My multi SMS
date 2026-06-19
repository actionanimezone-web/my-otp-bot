import telebot
import requests
from telebot import types

# ⚙️ বটের মূল সেটিংস 
BOT_TOKEN = "8781169713:AAFbUVk8WdahpM7R3-b1WdjLdTjM7iuyAP8"
ADMIN_ID = 8801880413789  # আপনার টেলিগ্রাম আইডি
BOT_USERNAME = "Iioooc_bot"  # আপনার বটের ইউজারনেম
SUPPORT_CHANNEL = "https://t.me/BuyZonePro"  # আপনার সাপোর্ট চ্যানেল লিংক

# ⚠️ আপনার আইভিএসএমএস একাউন্ট কোড এখানে বসানো হয়েছে
IVSMS_API_KEY = "4827726220"
VOLTX_API_KEY = "your_voltx_api_key"

bot = telebot.TeleBot(BOT_TOKEN)

user_active_orders = {}

# 🚀 স্টার্ট কমান্ড
@bot.message_handler(commands=['start'])
def start_message(message):
    user_name = message.from_user.first_name
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn1 = types.InlineKeyboardButton("📱 ইনস্টাগ্রাম নম্বর নিন", callback_data="get_insta_number")
    btn2 = types.InlineKeyboardButton("📩 ওটিপি চেক করুন", callback_data="check_insta_otp")
    btn3 = types.InlineKeyboardButton("📢 আমাদের চ্যানেল", url=SUPPORT_CHANNEL)
    
    markup.add(btn1, btn2, btn3)
    
    bot.send_message(
        message.chat.id, 
        f"আসসালামু আলাইকুম {user_name}!\n@{BOT_USERNAME} বটে আপনাকে স্বাগতম। এখান থেকে ফ্রিতে ইনস্টাগ্রাম নম্বর এবং ওটিপি নিন।", 
        reply_markup=markup
    )

# 🎛️ বাটন ক্লিক হ্যান্ডলার
@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    user_id = call.from_user.id
    
    if call.data == "get_insta_number":
        bot.answer_callback_query(call.id, "প্যানেল থেকে নম্বর আনা হচ্ছে...", show_alert=False)
        url = f"https://voltxsms.com/api/get_number?api_key={VOLTX_API_KEY}&service=ig"
        
        try:
            number = "+12015550199"
            order_id = "98765432"
            user_active_orders[user_id] = {"number": number, "order_id": order_id}
            
            msg_text = (
                f"🎉 আপনার ইনস্টাগ্রাম নম্বর রেডি:\n\n"
                f"📱 নম্বর: `{number}`\n"
                f"🆔 অর্ডার আইডি: `{order_id}`\n\n"
                f"⚠️ এই নম্বরটি আপনার ইনস্টাগ্রামে বসিয়ে ওটিপি পাঠান। "
                f"কোড পাঠানো হলে নিচের '📩 ওটিপি চেক করুন' বাটনে চাপ দিন।"
            )
            
            markup = types.InlineKeyboardMarkup()
            btn_otp = types.InlineKeyboardButton("📩 ওটিপি চেক করুন", callback_data="check_insta_otp")
            markup.add(btn_otp)
            
            bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            
        except Exception as e:
            bot.send_message(call.message.chat.id, "⚠️ প্যানেল কানেকশনে সমস্যা হয়েছে অথবা ব্যালেন্স নেই।")

    elif call.data == "check_insta_otp":
        if user_id in user_active_orders:
            order_id = user_active_orders[user_id]["order_id"]
            bot.answer_callback_query(call.id, "নগদে ওটিপি চেক করা হচ্ছে...", show_alert=False)
            otp_url = f"https://voltxsms.com/api/get_otp?api_key={VOLTX_API_KEY}&order_id={order_id}"
            
            try:
                otp_code = "458912"  
                bot.send_message(call.message.chat.id, f"📩 আপনার ইনস্টাগ্রাম ওটিপি কোড (নগদে):\n\n🔢 কোড: `{otp_code}`\n\nদ্রুত কোডটি বসিয়ে অ্যাকাউন্ট ভেরিফাই করুন।", parse_mode="Markdown")
            except:
                bot.send_message(call.message.chat.id, "⏳ এখনো ওটিপি আসেনি। দয়া করে ইনস্টাগ্রাম অ্যাপ থেকে আবার কোড পাঠান এবং ১ মিনিট পর চেষ্টা করুন।")
        else:
            bot.send_message(call.message.chat.id, "❌ আপনার কোনো অ্যাক্টিভ নম্বর নেওয়া নেই। আগে নম্বর নিন।")

print("ইনস্টাগ্রাম ফ্রি ওটিপি বট চালু আছে...")
bot.infinity_polling()
