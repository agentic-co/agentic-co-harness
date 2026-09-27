This document is a compiled Agentic Standard Operating Procedure (ASOP) for the SOPBench `bank` domain, produced deterministically from the domain's own dependency tables (`action_required_dependencies`, `action_customizable_dependencies`, `constraint_links`, `constraint_processes`) — no model authored any step or gate. Each procedure below corresponds to one agent-reachable bank action. Each numbered step names one precondition to verify, plus how to verify it, and the final step performs the action itself. The specific value each condition must resolve to is supplied separately per request; this document only names what to check.

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

1. **Complete the Login User action.** Call the login_user tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `login_user`)

## Procedure: Open Account

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the presence of outstanding owed balance condition.** whether the user has any outstanding owed balance. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_account_owed_balance`)
3. **Verify the presence of outstanding credit card balance condition.** whether the user has any outstanding balance on any credit card. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_credit_cards`)
4. **Complete the Open Account action.** Call the open_account tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `open_account`)

## Procedure: Authenticate Admin Password

1. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
2. **Complete the Authenticate Admin Password action.** Call the authenticate_admin_password tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `authenticate_admin_password`)

## Procedure: Set Admin Password

1. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
2. **Complete the Set Admin Password action.** Call the set_admin_password tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `set_admin_password`)

## Procedure: Set Account Information

1. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
2. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
3. **Complete the Set Account Information action.** Call the set_account_information tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `set_account_information`)

## Procedure: Close Account

1. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
2. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
3. **Complete the Close Account action.** Call the close_account tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `close_account`)

## Procedure: Get Account Balance

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Complete the Get Account Balance action.** Call the get_account_balance tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_account_balance`)

## Procedure: Transfer Funds

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
4. **Verify the account balance sufficiency condition.** the relationship between the account balance and the requested amount. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_account_balance`)
5. **Complete the Transfer Funds action.** Call the transfer_funds tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `transfer_funds`)

## Procedure: Deposit Funds

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the deposit amount against the maximum deposit limit condition.** the relationship between the requested deposit amount and the maximum deposit limit. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. This condition has no agent-callable verification action in this domain. Gate: judged
3. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
4. **Complete the Deposit Funds action.** Call the deposit_funds tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `deposit_funds`)

## Procedure: Pay Bill

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the account balance sufficiency condition.** the relationship between the account balance and the requested amount. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_account_balance`)
3. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
4. **Complete the Pay Bill action.** Call the pay_bill tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `pay_bill`)

## Procedure: Apply Credit Card

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the minimum eligible credit score condition.** the relationship between the user's credit score and the minimum eligible credit score. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_get_credit_score`)
3. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
4. **Complete the Apply Credit Card action.** Call the apply_credit_card tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `apply_credit_card`)

## Procedure: Exchange Foreign Currency

1. **Verify the foreign currency availability condition.** whether the requested foreign currency type is available at this bank. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_foreign_currency_available`)
2. **Verify the exchange amount against the maximum exchange limit condition.** the relationship between the requested exchange amount and the maximum exchange amount. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. This condition has no agent-callable verification action in this domain. Gate: judged
3. **Complete the Exchange Foreign Currency action.** Call the exchange_foreign_currency tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `exchange_foreign_currency`)

## Procedure: Get Account Owed Balance

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Complete the Get Account Owed Balance action.** Call the get_account_owed_balance tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_account_owed_balance`)

## Procedure: Get Loan

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Verify the loan eligibility owed-balance limit condition.** the relationship between the user's owed balance and the maximum owed balance allowed for a new loan. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_account_owed_balance`)
4. **Verify the minimum eligible credit score condition.** the relationship between the user's credit score and the minimum eligible credit score. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_get_credit_score`)
5. **Complete the Get Loan action.** Call the get_loan tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_loan`)

## Procedure: Pay Loan

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Verify the account-balance sufficiency for loan payoff condition.** the relationship between the user's account balance and their owed balance. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. This can alternatively be confirmed via get_account_owed_balance. Gate: deterministic (tool call: `get_account_balance`)
4. **Verify the account-balance sufficiency for the requested loan payment condition.** the relationship between the user's account balance and the requested loan payment amount. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_account_balance`)
5. **Complete the Pay Loan action.** Call the pay_loan tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `pay_loan`)

## Procedure: Get Safety Box

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
3. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
4. **Complete the Get Safety Box action.** Call the get_safety_box tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_safety_box`)

## Procedure: Set Safety Box

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
4. **Verify the safety box eligibility condition.** the relationship between the user's account balance and the minimum balance required for safety box eligibility. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `get_account_balance`)
5. **Verify the minimum eligible credit score condition.** the relationship between the user's credit score and the minimum eligible credit score. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_get_credit_score`)
6. **Complete the Set Safety Box action.** Call the set_safety_box tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `set_safety_box`)

## Procedure: Get Credit Cards

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the admin password authentication status condition.** whether the user has previously authenticated the admin password on this account. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `authenticate_admin_password`)
3. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
4. **Complete the Get Credit Cards action.** Call the get_credit_cards tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_credit_cards`)

## Procedure: Get Credit Card Info

1. **Verify the username existence condition.** whether the username exists in the database of accounts. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `internal_check_username_exist`)
2. **Verify the user login status condition.** whether the user has previously logged in with correct credentials. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. Gate: deterministic (tool call: `login_user`)
3. **Complete the Get Credit Card Info action.** Call the get_credit_card_info tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_credit_card_info`)

## Procedure: Get Bank Maximum Loan Amount

1. **Verify the bank database state condition.** the state of the bank database as a whole. The rules above state what this condition must be for this request; establish the actual value and confirm it against them before proceeding. This condition has no agent-callable verification action in this domain. Gate: judged
2. **Complete the Get Bank Maximum Loan Amount action.** Call the get_bank_maximum_loan_amount tool now that its prerequisites above have been verified. Gate: deterministic (tool call: `get_bank_maximum_loan_amount`)
