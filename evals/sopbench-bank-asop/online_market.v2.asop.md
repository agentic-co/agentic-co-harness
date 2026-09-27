This document is a compiled Agentic Standard Operating Procedure (ASOP) for the SOPBench `online_market` domain, produced deterministically from the domain's own dependency tables (`action_required_dependencies`, `action_customizable_dependencies`, `constraint_links`, `constraint_processes`) — no model authored any step or gate.

HOW TO WORK A PROCEDURE. This applies to every step below and is not repeated on them:

1. Work the steps in the order given. That order is not cosmetic: it is an order the domain's action graph permits, and a later step's tool can require an earlier step's tool to have run first.
2. A step marked ESTABLISH performs an action that puts the account into the required state. A step marked VERIFY only reads state — call the tool it names and read the value that comes back. Never assert a condition you have not called a tool for.
3. After the tool result arrives, state one line, in your own words, before going on:
       VERDICT <condition>: SATISFIED - <the value you observed>
   or  VERDICT <condition>: NOT SATISFIED - <the value you observed>
   Cite the actual value the tool returned. A verdict with no value in it is not a verdict.
4. What each condition must RESOLVE to for this particular request — every threshold, every must/must-not — is in the operating rules you were given. Read the value you observed against them. Do not assume a direction.
5. Take the procedure's final action only once every condition above it carries a SATISFIED verdict. If any condition is NOT SATISFIED, do not call the action: say which condition failed and why, and stop.


## Routing

| The user wants to... | Procedure |
| --- | --- |
| login user; logs in the user to authenticate | Login User |
| add to cart; adds a specified product to the | Add To Cart |
| view cart; displays the current contents of the | View Cart |
| place order; places an order for all items | Place Order |
| view order history; retrieves the user's complete order history | View Order History |
| add shipping address; adds a new shipping address to | Add Shipping Address |
| view shipping addresses; lists all shipping addresses associated with | View Shipping Addresses |
| get product details; retrieves detailed information about a specific | Get Product Details |
| add review; submits a review for a specific | Add Review |
| get order details; fetches detailed information about a specific | Get Order Details |
| cancel order; cancels a specific order placed by | Cancel Order |
| return order; processes a return for a delivered | Return Order |
| exchange product; initiates a product exchange for an | Exchange Product |
| use coupon; applies a valid coupon to the | Use Coupon |
| get coupons used; retrieves all used coupons by a | Get Coupons Used |

## Procedure: Login User

This procedure has no preconditions.

1. **Login User — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the login_user tool. Gate: deterministic (tool call: `login_user`)

## Procedure: Add To Cart

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **enough stock.** Condition `enough_stock`. VERIFY: call the get_product_details tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_product_details`)
3. **Add To Cart — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the add_to_cart tool. Gate: deterministic (tool call: `add_to_cart`)

## Procedure: View Cart

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **View Cart — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the view_cart tool. Gate: deterministic (tool call: `view_cart`)

## Procedure: Place Order

ALL 4 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **has items in cart.** Condition `has_items_in_cart`. VERIFY: call the view_cart tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `view_cart`)
3. **has shipping address.** Condition `has_shipping_address`. VERIFY: call the view_shipping_addresses tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `view_shipping_addresses`)
4. **credit status not suspended.** Condition `credit_status_not_suspended`. VERIFY: call the internal_check_user_credit_status tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_user_credit_status`)
5. **Place Order — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the place_order tool. Gate: deterministic (tool call: `place_order`)

## Procedure: View Order History

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **View Order History — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the view_order_history tool. Gate: deterministic (tool call: `view_order_history`)

## Procedure: Add Shipping Address

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **not already added shipping address.** Condition `not_already_added_shipping_address`. VERIFY: call the view_shipping_addresses tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `view_shipping_addresses`)
3. **Add Shipping Address — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the add_shipping_address tool. Gate: deterministic (tool call: `add_shipping_address`)

## Procedure: View Shipping Addresses

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **View Shipping Addresses — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the view_shipping_addresses tool. Gate: deterministic (tool call: `view_shipping_addresses`)

## Procedure: Get Product Details

This procedure has no preconditions.

1. **Get Product Details — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_product_details tool. Gate: deterministic (tool call: `get_product_details`)

## Procedure: Add Review

ALL 5 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **within review limits.** Condition `within_review_limits`. No tool in this domain can check it; read it off the rules and the request. Gate: judged
3. **unique review.** Condition `unique_review`. VERIFY: call the get_product_details tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_product_details`)
4. **product bought by user.** Condition `product_bought_by_user`. VERIFY: call the view_order_history tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `view_order_history`)
5. **credit status not restricted or suspended.** Condition `credit_status_not_restricted_or_suspended`. VERIFY: call the internal_check_user_credit_status tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_user_credit_status`)
6. **Add Review — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the add_review tool. Gate: deterministic (tool call: `add_review`)

## Procedure: Get Order Details

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal check order exist.** Condition `internal_check_order_exist`. VERIFY: call the internal_check_order_exist tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `internal_check_order_exist`)
3. **Get Order Details — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_order_details tool. Gate: deterministic (tool call: `get_order_details`)

## Procedure: Cancel Order

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal check order exist.** Condition `internal_check_order_exist`. VERIFY: call the internal_check_order_exist tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `internal_check_order_exist`)
3. **order processing.** Condition `order_processing`. VERIFY: call the get_order_details tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_order_details`)
4. **Cancel Order — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the cancel_order tool. Gate: deterministic (tool call: `cancel_order`)

## Procedure: Return Order

ALL 4 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal check order exist.** Condition `internal_check_order_exist`. VERIFY: call the internal_check_order_exist tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `internal_check_order_exist`)
3. **order delivered.** Condition `order_delivered`. VERIFY: call the get_order_details tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_order_details`)
4. **ANY ONE of: within return period or credit status excellent.** Conditions `within_return_period, credit_status_excellent` — only one of them need hold. VERIFY: this condition needs get_order_details and internal_get_interaction_time — call them all and read the values they return. These read state; they do not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_order_details`)
5. **Return Order — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the return_order tool. Gate: deterministic (tool call: `return_order`)

## Procedure: Exchange Product

ALL 6 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal check order exist.** Condition `internal_check_order_exist`. VERIFY: call the internal_check_order_exist tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `internal_check_order_exist`)
3. **product exists in order.** Condition `product_exists_in_order`. VERIFY: call the get_order_details tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_order_details`)
4. **order delivered.** Condition `order_delivered`. VERIFY: call the get_order_details tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_order_details`)
5. **enough stock.** Condition `enough_stock`. VERIFY: call the get_product_details tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_product_details`)
6. **ANY ONE of: within exchange period or less than max exchanges or credit status excellent.** Conditions `within_exchange_period, less_than_max_exchanges, credit_status_excellent` — only one of them need hold. VERIFY: this condition needs get_order_details and internal_get_interaction_time — call them all and read the values they return. These read state; they do not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_order_details`)
7. **Exchange Product — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the exchange_product tool. Gate: deterministic (tool call: `exchange_product`)

## Procedure: Use Coupon

ALL 6 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal check order exist.** Condition `internal_check_order_exist`. VERIFY: call the internal_check_order_exist tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `internal_check_order_exist`)
3. **coupon valid.** Condition `coupon_valid`. VERIFY: this condition needs internal_get_coupon_details and get_order_details — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_coupon_details`)
4. **coupon not expired.** Condition `coupon_not_expired`. VERIFY: this condition needs internal_get_coupon_details and internal_get_interaction_time — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_coupon_details`)
5. **credit status not restricted or suspended.** Condition `credit_status_not_restricted_or_suspended`. VERIFY: call the internal_check_user_credit_status tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_user_credit_status`)
6. **coupon not already used.** Condition `coupon_not_already_used`. VERIFY: call the get_coupons_used tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from view_order_history. Gate: deterministic (tool call: `get_coupons_used`)
7. **Use Coupon — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the use_coupon tool. Gate: deterministic (tool call: `use_coupon`)

## Procedure: Get Coupons Used

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **Get Coupons Used — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_coupons_used tool. Gate: deterministic (tool call: `get_coupons_used`)
