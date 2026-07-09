/* eslint-disable @typescript-eslint/no-empty-interface */
// Generated code for namespace: org.openclaw.intermediary@1.0.0

// imports
import {IConcept} from './concerto@1.0.0';

// interfaces
export interface IIntermediaryData extends IConcept {
 agreementNo?: string;
 signingPlace: string;
 partyAName: string;
 partyALegalRep: string;
 partyAAddress: string;
 partyAContact: string;
 partyBName: string;
 partyBLegalRep: string;
 partyBAddress: string;
 partyBContact: string;
 partyBRoleDescription: string;
 targetCountry: string;
 finderFeePercent: number;
 finderFeeCap: string;
 confidentialityTermYears: number;
 agreementTermMonths: number;
 terminationNoticeDays: number;
 breachCurePeriodDays: number;
 paymentTermDays: number;
 exclusivityExpiryMonths: number;
 governingLaw: string;
 arbitrationBody: string;
 effectiveDate: string;
}
