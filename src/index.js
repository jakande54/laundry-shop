// backend/index.js
const express = require("express");
const bodyParser = require("body-parser");
const fetch = require("node-fetch"); // or axios
const app = express();

app.use(bodyParser.json());

// Utility: get M-Pesa access token
async function getAccessToken() {
  const consumerKey = process.env.MPESA_CONSUMER_KEY;
  const consumerSecret = process.env.MPESA_CONSUMER_SECRET;
  const auth = Buffer.from(`${consumerKey}:${consumerSecret}`).toString("base64");

  const res = await fetch(
    "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials",
    {
      headers: { Authorization: `Basic ${auth}` },
    }
  );
  const data = await res.json();
  return data.access_token;
}

// Route: initiate STK push
app.post("/stk_push", async (req, res) => {
  const { order_id, phone, amount } = req.body;
  if (!order_id || !phone || !amount) {
    return res.json({ success: false, message: "Missing parameters" });
  }

  try {
    const token = await getAccessToken();
    const timestamp = new Date()
      .toISOString()
      .replace(/[-:TZ.]/g, "")
      .slice(0, 14);

    const shortcode = process.env.MPESA_SHORTCODE; // e.g. 174379 for sandbox
    const passkey = process.env.MPESA_PASSKEY;
    const password = Buffer.from(shortcode + passkey + timestamp).toString("base64");

    const payload = {
      BusinessShortCode: shortcode,
      Password: password,
      Timestamp: timestamp,
      TransactionType: "CustomerPayBillOnline",
      Amount: amount,
      PartyA: phone, // customer phone
      PartyB: shortcode,
      PhoneNumber: phone,
      CallBackURL: `${process.env.BASE_URL}/mpesa/callback`,
      AccountReference: `Order-${order_id}`,
      TransactionDesc: "Payment for order",
    };

    const stkRes = await fetch(
      "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      }
    );

    const data = await stkRes.json();
    if (data.ResponseCode === "0") {
      res.json({ success: true, message: "STK push initiated" });
    } else {
      res.json({ success: false, message: data.errorMessage || "Failed" });
    }
  } catch (err) {
    console.error(err);
    res.json({ success: false, message: "Server error" });
  }
});

// Route: M-Pesa callback
app.post("/mpesa/callback", (req, res) => {
  const body = req.body;
  console.log("Callback received:", JSON.stringify(body, null, 2));

  // Extract result
  const resultCode = body.Body.stkCallback.ResultCode;
  const orderRef = body.Body.stkCallback.CallbackMetadata?.Item?.find(
    (i) => i.Name === "AccountReference"
  )?.Value;

  if (resultCode === 0) {
    // Payment successful
    const orderId = orderRef?.split("-")[1];
    // Emit socket event to frontend
    io.emit("payment_status", { order_id: orderId, status: "paid" });
  } else {
    const orderId = orderRef?.split("-")[1];
    io.emit("payment_status", {
      order_id: orderId,
      status: "failed",
      reason: body.Body.stkCallback.ResultDesc,
    });
  }

  res.json({ ResultCode: 0, ResultDesc: "Callback received successfully" });
});

const server = app.listen(3000, () => {
  console.log("Backend running on port 3000");
});

// Attach socket.io
const io = require("socket.io")(server);
