//! Shared Inference stakes: nodes' collateral, job payouts, and slashes.
//!
//! An account's wallet stakes SOL as collateral for its nodes. The coordinator pays
//! each finished job into the stakes of the nodes that ran it, releases earnings back
//! to the wallet once their audits are past (and the collateral once the nodes have
//! left), and burns a stake when an audit shows its nodes faked work. See
//! docs/solana.md.

use anchor_lang::prelude::*;
use anchor_lang::system_program::{transfer, Transfer};

declare_id!("97VaN9bk5Ee57BFaW5UVoojHjhy3K16RvRcZEnu5cULU"); // data/solana/stakes-keypair.json (scripts/misc/deploy-solana.sh)

/// Slashed SOL goes here, so nobody profits from a slash
pub const INCINERATOR: Pubkey = pubkey!("1nc1nerator11111111111111111111111111111111");

#[program]
pub mod stakes {
    use super::*;

    /// Once, right after deploying: the coordinator's key
    pub fn initialize(ctx: Context<Initialize>) -> Result<()> {
        let config = &mut ctx.accounts.config;
        config.authority = ctx.accounts.authority.key();
        config.bump = ctx.bumps.config;
        Ok(())
    }

    /// Collateral from an account's wallet for its nodes (the account: its id on the site),
    /// creating the stake the first time
    pub fn stake(ctx: Context<Deposit>, account: [u8; 16], amount: u64) -> Result<()> {
        let stake = &mut ctx.accounts.stake;
        require!(!stake.banned, StakeError::Banned);
        stake.wallet = ctx.accounts.wallet.key();
        stake.account = account;
        stake.bump = ctx.bumps.stake;

        let accounts = Transfer { from: ctx.accounts.wallet.to_account_info(), to: stake.to_account_info() };
        transfer(CpiContext::new(System::id(), accounts), amount)
    }



    /***************
     * Coordinator *
     ***************/

    /// A node owner's share of a job, from the vault, w/ the job's id and its record's hash
    pub fn pay(ctx: Context<Pay>, job: [u8; 16], record: [u8; 32], amount: u64) -> Result<()> {
        require!(!ctx.accounts.stake.banned, StakeError::Banned);
        require!(amount <= spare(&ctx.accounts.config.to_account_info())?, StakeError::VaultEmpty);

        ctx.accounts.config.sub_lamports(amount)?;
        ctx.accounts.stake.add_lamports(amount)?;
        emit!(Paid { job, record, stake: ctx.accounts.stake.key(), amount });
        Ok(())
    }

    /// Back to the stake's own wallet: earnings past their audits, or the collateral of a node that left
    pub fn release(ctx: Context<Release>, amount: u64) -> Result<()> {
        require!(!ctx.accounts.stake.banned, StakeError::Banned);
        require!(amount <= spare(&ctx.accounts.stake.to_account_info())?, StakeError::TooMuch);

        ctx.accounts.stake.sub_lamports(amount)?;
        ctx.accounts.wallet.add_lamports(amount)?;
        emit!(Released { stake: ctx.accounts.stake.key(), amount });
        Ok(())
    }

    /// An audit found fraud: burn the whole stake and ban it, w/ the job's id and its evidence's hash
    pub fn slash(ctx: Context<Slash>, job: [u8; 16], evidence: [u8; 32]) -> Result<()> {
        let amount = spare(&ctx.accounts.stake.to_account_info())?;
        ctx.accounts.stake.sub_lamports(amount)?;
        ctx.accounts.incinerator.add_lamports(amount)?;

        let stake = &mut ctx.accounts.stake;
        stake.banned = true;
        emit!(Slashed { job, evidence, stake: stake.key(), amount });
        Ok(())
    }
}

/// Lamports above the account's rent-exempt minimum
fn spare(account: &AccountInfo) -> Result<u64> {
    Ok(account.lamports().saturating_sub(Rent::get()?.minimum_balance(account.data_len())))
}


/************
 * Accounts *
 ************/

/// The coordinator's key; also the vault payouts come from (fund it w/ a plain transfer)
#[account]
#[derive(InitSpace)]
pub struct Config {
    pub authority: Pubkey,
    pub bump: u8,
}

/// One per (wallet, account): collateral + earnings not released yet
#[account]
#[derive(InitSpace)]
pub struct Stake {
    pub wallet: Pubkey,
    pub account: [u8; 16],
    pub banned: bool,
    pub bump: u8,
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(mut)]
    pub authority: Signer<'info>,
    #[account(init, payer = authority, space = 8 + Config::INIT_SPACE, seeds = [b"config"], bump)]
    pub config: Account<'info, Config>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
#[instruction(account: [u8; 16])]
pub struct Deposit<'info> {
    #[account(mut)]
    pub wallet: Signer<'info>,
    #[account(init_if_needed, payer = wallet, space = 8 + Stake::INIT_SPACE,
              seeds = [b"stake", wallet.key().as_ref(), account.as_ref()], bump)]
    pub stake: Account<'info, Stake>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Pay<'info> {
    pub authority: Signer<'info>,
    #[account(mut, has_one = authority, seeds = [b"config"], bump = config.bump)]
    pub config: Account<'info, Config>,
    #[account(mut)]
    pub stake: Account<'info, Stake>,
}

#[derive(Accounts)]
pub struct Release<'info> {
    pub authority: Signer<'info>,
    #[account(has_one = authority, seeds = [b"config"], bump = config.bump)]
    pub config: Account<'info, Config>,
    #[account(mut, has_one = wallet)]
    pub stake: Account<'info, Stake>,
    /// CHECK: the stake's own wallet (has_one), only receives SOL
    #[account(mut)]
    pub wallet: UncheckedAccount<'info>,
}

#[derive(Accounts)]
pub struct Slash<'info> {
    pub authority: Signer<'info>,
    #[account(has_one = authority, seeds = [b"config"], bump = config.bump)]
    pub config: Account<'info, Config>,
    #[account(mut)]
    pub stake: Account<'info, Stake>,
    /// CHECK: only receives the burned SOL
    #[account(mut, address = INCINERATOR)]
    pub incinerator: UncheckedAccount<'info>,
}


/**********
 * Events *
 **********/

#[event]
pub struct Paid {
    pub job: [u8; 16],
    pub record: [u8; 32],
    pub stake: Pubkey,
    pub amount: u64,
}

#[event]
pub struct Released {
    pub stake: Pubkey,
    pub amount: u64,
}

#[event]
pub struct Slashed {
    pub job: [u8; 16],
    pub evidence: [u8; 32],
    pub stake: Pubkey,
    pub amount: u64,
}

#[error_code]
pub enum StakeError {
    #[msg("This stake was slashed.")]
    Banned,
    #[msg("More than the stake holds.")]
    TooMuch,
    #[msg("The vault can't cover this payout.")]
    VaultEmpty,
}
