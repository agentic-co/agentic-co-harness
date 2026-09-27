This document is a compiled Agentic Standard Operating Procedure (ASOP) for the SOPBench `bank` domain, produced deterministically from the domain's own dependency tables (`action_required_dependencies`, `action_customizable_dependencies`, `constraint_links`, `constraint_processes`) — no model authored any step or gate.

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
| open account; creates and opens an account with | Open Account |
| authenticate admin password; verifies that the entered admin password | Authenticate Admin Password |
| set admin password; sets the admin password for their | Set Admin Password |
| set account information; sets the information for their account | Set Account Information |
| close account; closes the account and deletes all | Close Account |
| get account balance; retrieves the bank account balance of | Get Account Balance |
| transfer funds; transfers the funds from the current | Transfer Funds |
| deposit funds; deposits the amount of funds listed | Deposit Funds |
| pay bill; pays a bill from an account | Pay Bill |
| apply credit card; the user applies for a credit | Apply Credit Card |
| exchange foreign currency; exchanges some usd for some specified | Exchange Foreign Currency |
| get account owed balance; retrieves the bank account owed balance | Get Account Owed Balance |
| get loan; the user applies for a loan | Get Loan |
| pay loan; the user pays off a portion | Pay Loan |
| get safety box; gets the contents of the safety | Get Safety Box |
| set safety box; sets the contents of the safety | Set Safety Box |
| get credit cards; gets a list of the credit | Get Credit Cards |
| get credit card info; gets the information of a specific | Get Credit Card Info |
| get bank maximum loan amount; shows the maximum amount of money | Get Bank Maximum Loan Amount |

## Procedure: Login User

This procedure has no preconditions.

1. **Login User — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the login_user tool. Gate: deterministic (tool call: `login_user`)

## Procedure: Open Account

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **presence of outstanding owed balance.** Condition `no_owed_balance`. VERIFY: call the get_account_owed_balance tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_account_owed_balance`)
3. **presence of outstanding credit card balance.** Condition `no_credit_card_balance`. VERIFY: call the get_credit_cards tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_credit_cards`)
4. **Open Account — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the open_account tool. Gate: deterministic (tool call: `open_account`)

## Procedure: Authenticate Admin Password

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **Authenticate Admin Password — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the authenticate_admin_password tool. Gate: deterministic (tool call: `authenticate_admin_password`)

## Procedure: Set Admin Password

One condition must hold before the final action.

1. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
2. **Set Admin Password — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the set_admin_password tool. Gate: deterministic (tool call: `set_admin_password`)

## Procedure: Set Account Information

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
3. **Set Account Information — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the set_account_information tool. Gate: deterministic (tool call: `set_account_information`)

## Procedure: Close Account

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
3. **Close Account — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the close_account tool. Gate: deterministic (tool call: `close_account`)

## Procedure: Get Account Balance

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **Get Account Balance — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_account_balance tool. Gate: deterministic (tool call: `get_account_balance`)

## Procedure: Transfer Funds

ALL 4 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
4. **account balance sufficiency.** Condition `sufficient_account_balance`. VERIFY: call the get_account_balance tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_account_balance`)
5. **Transfer Funds — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the transfer_funds tool. Gate: deterministic (tool call: `transfer_funds`)

## Procedure: Deposit Funds

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **deposit amount against the maximum deposit limit.** Condition `maximum_deposit_limit`. No tool in this domain can check it; read it off the rules and the request. Gate: judged
3. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
4. **Deposit Funds — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the deposit_funds tool. Gate: deterministic (tool call: `deposit_funds`)

## Procedure: Pay Bill

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **account balance sufficiency.** Condition `sufficient_account_balance`. VERIFY: call the get_account_balance tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_account_balance`)
4. **Pay Bill — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the pay_bill tool. Gate: deterministic (tool call: `pay_bill`)

## Procedure: Apply Credit Card

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **minimum eligible credit score.** Condition `minimal_elgibile_credit_score`. VERIFY: call the internal_get_credit_score tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_credit_score`)
3. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
4. **Apply Credit Card — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the apply_credit_card tool. Gate: deterministic (tool call: `apply_credit_card`)

## Procedure: Exchange Foreign Currency

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **foreign currency availability.** Condition `internal_check_foreign_currency_available`. VERIFY: call the internal_check_foreign_currency_available tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_foreign_currency_available`)
2. **exchange amount against the maximum exchange limit.** Condition `maximum_exchange_amount`. No tool in this domain can check it; read it off the rules and the request. Gate: judged
3. **Exchange Foreign Currency — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the exchange_foreign_currency tool. Gate: deterministic (tool call: `exchange_foreign_currency`)

## Procedure: Get Account Owed Balance

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **Get Account Owed Balance — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_account_owed_balance tool. Gate: deterministic (tool call: `get_account_owed_balance`)

## Procedure: Get Loan

ALL 4 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **loan eligibility owed-balance limit.** Condition `get_loan_owed_balance_restr`. VERIFY: call the get_account_owed_balance tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_account_owed_balance`)
4. **minimum eligible credit score.** Condition `minimal_elgibile_credit_score`. VERIFY: call the internal_get_credit_score tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_credit_score`)
5. **Get Loan — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_loan tool. Gate: deterministic (tool call: `get_loan`)

## Procedure: Pay Loan

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **ANY ONE of: account-balance sufficiency for loan payoff or account-balance sufficiency for the requested loan payment.** Conditions `pay_loan_account_balance_restr, pay_loan_amount_restr` — only one of them need hold. VERIFY: this condition needs get_account_balance and get_account_owed_balance — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `get_account_balance`)
4. **Pay Loan — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the pay_loan tool. Gate: deterministic (tool call: `pay_loan`)

## Procedure: Get Safety Box

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
4. **Get Safety Box — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_safety_box tool. Gate: deterministic (tool call: `get_safety_box`)

## Procedure: Set Safety Box

ALL 5 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
4. **safety box eligibility.** Condition `safety_box_eligible`. VERIFY: call the get_account_balance tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `get_account_balance`)
5. **minimum eligible credit score.** Condition `minimal_elgibile_credit_score`. VERIFY: call the internal_get_credit_score tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_credit_score`)
6. **Set Safety Box — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the set_safety_box tool. Gate: deterministic (tool call: `set_safety_box`)

## Procedure: Get Credit Cards

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **admin password authentication status.** Condition `authenticated_admin_password`. ESTABLISH: call the authenticate_admin_password tool, which puts the account into this state. Gate: deterministic (tool call: `authenticate_admin_password`)
4. **Get Credit Cards — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_credit_cards tool. Gate: deterministic (tool call: `get_credit_cards`)

## Procedure: Get Credit Card Info

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **username existence.** Condition `internal_check_username_exist`. VERIFY: call the internal_check_username_exist tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool, which puts the account into this state. Gate: deterministic (tool call: `login_user`)
3. **Get Credit Card Info — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_credit_card_info tool. Gate: deterministic (tool call: `get_credit_card_info`)

## Procedure: Get Bank Maximum Loan Amount

One condition must hold before the final action.

1. **bank database state.** Condition `call_get_database`. No tool in this domain can check it; read it off the rules and the request. Gate: judged
2. **Get Bank Maximum Loan Amount — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_bank_maximum_loan_amount tool. Gate: deterministic (tool call: `get_bank_maximum_loan_amount`)
